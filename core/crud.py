"""Tiny generic list/create/edit/delete views for simple lookup tables (departments, shifts, ...)."""
from django.contrib import messages
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path

from .decorators import staff_required
from .models import ActivityLog


class SimpleCrud:
    def __init__(self, model, form_class, prefix, title, columns, help_text="", ordering=None):
        self.model = model
        self.form_class = form_class
        self.prefix = prefix
        self.title = title
        self.columns = columns  # list of (label, attribute or callable)
        self.help_text = help_text
        self.ordering = ordering
        self.list_name = f"{prefix}_list"
        self.create_name = f"{prefix}_create"
        self.edit_name = f"{prefix}_edit"
        self.delete_name = f"{prefix}_delete"

    def _rows(self, objs):
        rows = []
        for o in objs:
            cells = []
            for _, attr in self.columns:
                value = attr(o) if callable(attr) else getattr(o, attr)
                cells.append(value)
            rows.append((o, cells))
        return rows

    def list_view(self, request):
        qs = self.model.objects.all()
        if self.ordering:
            qs = qs.order_by(*self.ordering)
        return render(request, "core/simple_list.html", {
            "crud": self, "rows": self._rows(qs), "headers": [c[0] for c in self.columns],
        })

    def form_view(self, request, pk=None):
        obj = get_object_or_404(self.model, pk=pk) if pk else None
        form = self.form_class(request.POST or None, instance=obj)
        if request.method == "POST" and form.is_valid():
            obj = form.save()
            ActivityLog.log(request, f"{self.title}: {'updated' if pk else 'created'}", obj)
            messages.success(request, f"{self.model._meta.verbose_name.capitalize()} saved.")
            return redirect(f"{self.prefix}_list")
        return render(request, "core/simple_form.html", {"crud": self, "form": form, "object": obj})

    def delete_view(self, request, pk):
        obj = get_object_or_404(self.model, pk=pk)
        if request.method == "POST":
            try:
                obj.delete()
                ActivityLog.log(request, f"{self.title}: deleted", obj)
                messages.success(request, "Deleted.")
            except ProtectedError:
                messages.error(request, "This item is in use and cannot be deleted.")
        return redirect(f"{self.prefix}_list")

    def urls(self, route):
        return [
            path(f"{route}/", staff_required(self.list_view), name=f"{self.prefix}_list"),
            path(f"{route}/new/", staff_required(self.form_view), name=f"{self.prefix}_create"),
            path(f"{route}/<int:pk>/edit/", staff_required(self.form_view), name=f"{self.prefix}_edit"),
            path(f"{route}/<int:pk>/delete/", staff_required(self.delete_view), name=f"{self.prefix}_delete"),
        ]
