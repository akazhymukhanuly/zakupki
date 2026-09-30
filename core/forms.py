from django import forms
from django.contrib.auth.models import User
from django.db.models import Q

from . import roles
from .models import Contract, Memo, MemoItem, Nomenclature, Procurement, Supplier


def user_label(u):
    return u.get_full_name() or u.username


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


def bootstrapify(form):
    for name, field in form.fields.items():
        w = field.widget
        if isinstance(w, forms.CheckboxSelectMultiple):
            continue
        if isinstance(w, forms.CheckboxInput):
            w.attrs.setdefault("class", "form-check-input")
        elif isinstance(w, (forms.Select, forms.SelectMultiple)):
            w.attrs.setdefault("class", "form-select")
        else:
            w.attrs.setdefault("class", "form-control")
    return form


class BootstrapMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        bootstrapify(self)


class MemoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Memo
        fields = ["department", "justification", "required_date", "approver"]
        widgets = {"required_date": DateInput(), "justification": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["approver"].queryset = User.objects.filter(groups__name__in=[roles.APPROVER, roles.DIRECTOR]).distinct()
        self.fields["approver"].required = False
        self.fields["approver"].help_text = "Если не указан — руководитель подразделения"
        self.fields["approver"].label_from_instance = user_label


class MemoItemForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = MemoItem
        fields = ["nomenclature", "description", "quantity", "unit", "required_date", "category", "budget_item",
                  "estimated_price", "urgent"]
        widgets = {"required_date": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["nomenclature"].required = False
        self.fields["description"].required = False
        self.fields["unit"].required = False
        self.fields["nomenclature"].queryset = Nomenclature.objects.select_related("category")
        if not self.instance.pk:
            self.initial.setdefault("unit", "")
        self.fields["unit"].widget.attrs["placeholder"] = "из номенклатуры"

    def clean(self):
        data = super().clean()
        nom = data.get("nomenclature")
        if not data.get("description"):
            if not nom:
                raise forms.ValidationError("Укажите номенклатуру из справочника или описание.")
            data["description"] = nom.name
        if nom and not data.get("category"):
            data["category"] = nom.category
        if not data.get("unit"):
            data["unit"] = nom.unit if nom else "шт"
        if data.get("quantity") is not None and data["quantity"] <= 0:
            self.add_error("quantity", "Количество должно быть больше нуля.")
        return data


class ProcurementForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Procurement
        fields = ["title", "buyer", "method", "kp_deadline"]
        widgets = {"kp_deadline": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["buyer"].queryset = User.objects.filter(
            Q(groups__name=roles.BUYER) | Q(is_superuser=True), is_active=True
        ).distinct()
        self.fields["buyer"].label_from_instance = user_label
        self.fields["title"].required = False  # при создании из пула подставляется по категориям


class ContractForm(BootstrapMixin, forms.ModelForm):
    """Ручное создание договора (в MVP — для рамочных договоров)."""

    class Meta:
        model = Contract
        fields = ["number", "date", "kind", "supplier", "valid_until", "status"]
        widgets = {"date": DateInput(), "valid_until": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["kind"].choices = [(Contract.Kind.FRAMEWORK, "Рамочный договор")]
        self.fields["status"].choices = [(s, l) for s, l in Contract.Status.choices
                                         if s in (Contract.Status.DRAFT, Contract.Status.SIGNED)]


class SupplierForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Supplier
        fields = ["name", "bin", "email", "phone", "contact", "categories", "code_1c"]
        widgets = {"categories": forms.CheckboxSelectMultiple}


class UploadForm(forms.Form):
    file = forms.FileField(label="Файл", widget=forms.ClearableFileInput(attrs={"class": "form-control"}))
