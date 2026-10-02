"""Pagination with a user-selectable page size (remembered per list in the session)."""
from django.core.paginator import Paginator

PER_PAGE_CHOICES = [25, 50, 100, 200, 500]
ALL_LIMIT = 2000  # "All" is offered only for lists up to this many rows


def paginate(request, items, default=50, key=None):
    key = f"per_page:{key or (request.resolver_match.view_name if request.resolver_match else request.path)}"
    raw = request.GET.get("per_page", "")
    if raw == "all":
        per = "all"
    elif raw.isdigit() and int(raw) in PER_PAGE_CHOICES:
        per = int(raw)
    else:
        per = request.session.get(key, default)
    if raw:
        request.session[key] = per
    count = items.count() if hasattr(items, "count") and not isinstance(items, list) else len(items)
    if per == "all" and count > ALL_LIMIT:
        per = max(PER_PAGE_CHOICES)
    size = max(count, 1) if per == "all" else per
    page = Paginator(items, size).get_page(request.GET.get("page"))
    page.per_page_value = per
    page.per_page_choices = PER_PAGE_CHOICES
    page.allow_all = count <= ALL_LIMIT
    return page
