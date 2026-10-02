"""Login details (user accounts, password hashes, sessions) in backups and restores.

* Backups can leave them out: `strip_logins_sqlite()` removes them from a snapshot copy.
* Restores can keep the current ones: `capture_logins()` before the database is replaced and
  `reapply_logins()` afterwards. Everyone keeps today's password and stays signed in; "created by" /
  "approved by" references in the restored data are matched to the same user names.
"""
import sqlite3
from collections import defaultdict

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection, transaction

NOLOGINS_TAG = "-nologins"


def user_reference_fields():
    """(model, field) for every concrete column pointing at a user account (incl. auto-created M2M tables)."""
    User = get_user_model()
    for model in apps.get_models(include_auto_created=True):
        if model is User:
            continue
        for field in model._meta.concrete_fields:
            if field.is_relation and field.related_model is User:
                yield model, field


def strip_logins_sqlite(path, log=lambda m: None):
    """Remove all login details from the SQLite file at `path` (a snapshot copy, never the live database)."""
    User = get_user_model()
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        with con:
            for model, field in user_reference_fields():
                table, column = model._meta.db_table, field.column
                if field.null:
                    con.execute(f'UPDATE "{table}" SET "{column}" = NULL')
                else:  # e.g. user<->group links, admin history: rows only exist because of a user
                    con.execute(f'DELETE FROM "{table}"')
            con.execute('DELETE FROM "django_session"')
            con.execute(f'DELETE FROM "{User._meta.db_table}"')
        con.execute("VACUUM")  # really remove the deleted rows (password hashes) from the file
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("integrity check failed after removing login details")
    finally:
        con.close()
    log("Login details (user accounts, passwords, sessions) left out of this backup")


def is_nologins_backup(name):
    return NOLOGINS_TAG + "." in name


# ---------------------------------------------------------------- keeping current logins during a restore
def capture_logins():
    """Snapshot of the current accounts, group memberships, employee login links and sessions."""
    from django.contrib.sessions.models import Session

    from employees.models import Employee
    User = get_user_model()
    users = []
    for user in User.objects.all().prefetch_related("groups"):
        row = {f.attname: getattr(user, f.attname) for f in User._meta.concrete_fields}
        row["_groups"] = [g.name for g in user.groups.all()]
        users.append(row)
    links = dict(Employee.objects.filter(user__isnull=False).values_list("device_user_id", "user__username"))
    sessions = list(Session.objects.values("session_key", "session_data", "expire_date"))
    return {"users": users, "employee_links": links, "sessions": sessions}


def reapply_logins(saved, log=lambda m: None):
    """Replace the accounts of the (just restored) database with the captured ones."""
    from django.contrib.auth.models import Group
    from django.contrib.sessions.models import Session
    from django.core.management.color import no_style

    from employees.models import Employee
    User = get_user_model()
    restored_names = dict(User.objects.values_list("id", "username"))
    references = []  # (model, field, pk, old user id) of records that point at a user of the restored data
    for model, field in user_reference_fields():
        if model._meta.auto_created:
            continue  # M2M rows (user groups/permissions) are rebuilt below
        for pk, user_id in model.objects.exclude(**{field.attname: None}).values_list("pk", field.attname):
            references.append((model, field, pk, user_id))

    with transaction.atomic():
        User.objects.all().delete()  # references are set to NULL (or removed for admin history)
        new_ids = {}
        for row in saved["users"]:
            row = dict(row)
            groups = row.pop("_groups", [])
            user = User(**row)
            user.save(force_insert=True)
            user.groups.set(Group.objects.filter(name__in=groups))
            new_ids[user.username] = user.pk
        with connection.cursor() as cur:  # PostgreSQL: continue user ids after the re-inserted ones
            for sql in connection.ops.sequence_reset_sql(no_style(), [User]):
                cur.execute(sql)

        remap = defaultdict(list)
        for model, field, pk, old_id in references:
            new_id = new_ids.get(restored_names.get(old_id))
            if new_id is not None:
                remap[(model, field.attname, new_id)].append(pk)
        for (model, attname, new_id), pks in remap.items():
            if model is Employee and attname == "user_id":
                continue  # handled below (one login per employee)
            model.objects.filter(pk__in=pks).update(**{attname: new_id})

        linked = 0
        taken = set()
        for device_user_id, username in saved["employee_links"].items():
            user_id = new_ids.get(username)
            if user_id and user_id not in taken:
                linked += Employee.objects.filter(device_user_id=device_user_id).update(user_id=user_id)
                taken.add(user_id)

        Session.objects.all().delete()
        Session.objects.bulk_create(Session(**s) for s in saved["sessions"])
    log(f"Kept the current {len(saved['users'])} user account(s) and passwords "
        f"({linked} employee self-service login(s) re-linked)")
