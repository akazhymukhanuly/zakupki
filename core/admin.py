from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from .models import (
    ApprovalRule, BudgetItem, Category, Contract, ContractLine, Department, Memo, MemoItem, MemoTemplate,
    MemoTemplateLine, Nomenclature, Payment, Procurement, ProcurementLine, Profile, Receipt, ReceiptLine, Supplier,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    """Документы меняются только через интерфейс системы — иначе нарушатся правила и статусы.
    В админке их можно только смотреть."""

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyInline:
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


admin.site.unregister(User)


@admin.register(User)
class UserWithProfileAdmin(UserAdmin):
    inlines = [ProfileInline]
    list_display = ["username", "first_name", "last_name", "roles", "is_active"]

    @admin.display(description="Роли")
    def roles(self, obj):
        return ", ".join(obj.groups.values_list("name", flat=True))


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ["name", "head"]


@admin.register(Nomenclature)
class NomenclatureAdmin(admin.ModelAdmin):
    list_display = ["name", "unit", "category", "code_1c"]
    list_filter = ["category"]
    search_fields = ["name", "code_1c"]


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ["name", "bin", "email", "phone"]
    filter_horizontal = ["categories"]
    search_fields = ["name", "bin"]


@admin.register(ApprovalRule)
class ApprovalRuleAdmin(admin.ModelAdmin):
    list_display = ["min_amount", "role"]


class TemplateLineInline(admin.TabularInline):
    model = MemoTemplateLine
    extra = 1


@admin.register(MemoTemplate)
class MemoTemplateAdmin(admin.ModelAdmin):
    inlines = [TemplateLineInline]
    list_display = ["name", "owner", "department"]


class MemoItemInline(ReadOnlyInline, admin.TabularInline):
    model = MemoItem
    extra = 0
    fields = ["line_no", "description", "quantity", "unit", "required_date", "status"]
    readonly_fields = ["status"]


@admin.register(Memo)
class MemoAdmin(ReadOnlyAdmin):
    list_display = ["number", "department", "initiator", "state", "required_date"]
    list_filter = ["state", "department"]
    inlines = [MemoItemInline]


class ProcurementLineInline(ReadOnlyInline, admin.TabularInline):
    model = ProcurementLine
    extra = 0
    readonly_fields = ["item", "quantity", "state"]


@admin.register(Procurement)
class ProcurementAdmin(ReadOnlyAdmin):
    list_display = ["number", "title", "buyer", "status", "decision_state"]
    list_filter = ["status"]
    inlines = [ProcurementLineInline]


class ContractLineInline(ReadOnlyInline, admin.TabularInline):
    model = ContractLine
    extra = 0


@admin.register(Contract)
class ContractAdmin(ReadOnlyAdmin):
    list_display = ["number", "kind", "supplier", "status", "date"]
    list_filter = ["status", "kind"]
    inlines = [ContractLineInline]


admin.site.register([BudgetItem, Category])


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ["contract", "date", "amount", "doc_number"]


class ReceiptLineInline(ReadOnlyInline, admin.TabularInline):
    model = ReceiptLine
    extra = 0


@admin.register(Receipt)
class ReceiptAdmin(ReadOnlyAdmin):
    list_display = ["contract", "date", "doc_number"]
    inlines = [ReceiptLineInline]
