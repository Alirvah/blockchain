from decimal import Decimal

from django import forms
from django.contrib.auth.models import User

from .models import Transfer, Wallet


class WalletCreateForm(forms.Form):
    label = forms.CharField(max_length=100)
    owner = forms.ModelChoiceField(
        queryset=User.objects.all(),
        required=False,
        help_text="Leave blank for an unassigned wallet",
    )

    def save(self):
        wallet = Wallet.objects.create(
            label=self.cleaned_data["label"],
            wallet_type=Wallet.CUSTOMER,
            owner=self.cleaned_data.get("owner"),
        )
        return wallet


class TransferForm(forms.Form):
    sender = forms.ModelChoiceField(
        queryset=Wallet.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    recipient = forms.ModelChoiceField(
        queryset=Wallet.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    amount = forms.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-input", "step": "0.01"}),
    )
    memo = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )

    def clean(self):
        cleaned = super().clean()
        sender = cleaned.get("sender")
        recipient = cleaned.get("recipient")
        amount = cleaned.get("amount")

        if sender and recipient and sender == recipient:
            raise forms.ValidationError("Sender and recipient must be different.")

        if sender and amount:
            if sender.balance < amount:
                raise forms.ValidationError(
                    f"Insufficient balance. {sender.label} has {sender.balance:,.2f} PAT."
                )

        return cleaned


class CustomerTransferForm(forms.Form):
    """Simplified transfer form for customers sending from their wallet."""

    recipient_address = forms.CharField(
        max_length=42,
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "0x..."}),
    )
    amount = forms.DecimalField(
        max_digits=18,
        decimal_places=2,
        min_value=Decimal("0.01"),
        widget=forms.NumberInput(attrs={"class": "form-input", "step": "0.01"}),
    )
    memo = forms.CharField(
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"class": "form-input", "placeholder": "Optional memo"}),
    )

    def __init__(self, *args, sender_wallet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sender_wallet = sender_wallet

    def clean_recipient_address(self):
        address = self.cleaned_data["recipient_address"]
        try:
            return Wallet.objects.get(address=address)
        except Wallet.DoesNotExist:
            raise forms.ValidationError("No wallet found with that address.")

    def clean(self):
        cleaned = super().clean()
        amount = cleaned.get("amount")
        recipient = cleaned.get("recipient_address")

        if self.sender_wallet and recipient and self.sender_wallet == recipient:
            raise forms.ValidationError("Cannot send to your own wallet.")

        if self.sender_wallet and amount:
            if self.sender_wallet.balance < amount:
                raise forms.ValidationError(
                    f"Insufficient balance. You have {self.sender_wallet.balance:,.2f} PAT."
                )

        return cleaned


class UserCreateForm(forms.Form):
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password = forms.CharField(widget=forms.PasswordInput)
    create_wallet = forms.BooleanField(required=False, initial=True)

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already taken.")
        return username

    def save(self):
        user = User.objects.create_user(
            username=self.cleaned_data["username"],
            email=self.cleaned_data.get("email", ""),
            password=self.cleaned_data["password"],
        )
        wallet = None
        if self.cleaned_data.get("create_wallet"):
            wallet = Wallet.objects.create(
                label=f"{user.username}'s Wallet",
                wallet_type=Wallet.CUSTOMER,
                owner=user,
            )
        return user, wallet


class InviteCreateForm(forms.Form):
    note = forms.CharField(
        max_length=120,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-input",
                "placeholder": "Optional note for who this invite is for",
            }
        ),
    )


class InviteRegistrationForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-input"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-input"}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-input"}),
    )
    confirm_password = forms.CharField(
        widget=forms.PasswordInput(attrs={"class": "form-input"}),
    )

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already taken.")
        return username

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm_password = cleaned.get("confirm_password")
        if password and confirm_password and password != confirm_password:
            raise forms.ValidationError("Passwords do not match.")
        return cleaned
