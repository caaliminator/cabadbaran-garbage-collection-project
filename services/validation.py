"""
Shared form validation.

Server-side validation is the real gate -- the browser's `required` and
`minlength` attributes are a convenience for the person filling the form in,
not a security control. Anything enforced in the templates is enforced again
here, because a form post can be crafted by hand.

`ValidationError` carries a dict of {field: message} so a route can hand the
messages straight back to the template and mark the offending inputs, rather
than losing everything to one generic "invalid input" flash.
"""

import re

USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
VEHICLE_RE = re.compile(r"^(TRI|TRK)-\d{2,3}$")


class ValidationError(Exception):
    """One or more fields failed. `errors` maps field name -> message."""

    def __init__(self, errors, field: str = "form"):
        if isinstance(errors, str):
            errors = {field: errors}
        super().__init__("; ".join(errors.values()))
        self.errors = dict(errors)

    @property
    def message(self) -> str:
        """One line suitable for a flash message."""
        return " ".join(self.errors.values())


class Validator:
    """
    Collects every failure instead of raising on the first one, so the person
    filling in the form sees all their mistakes at once rather than one per
    submission.
    """

    def __init__(self, form=None):
        self.form = form or {}
        self.errors: dict[str, str] = {}
        self.warnings: list[str] = []
        self.data: dict[str, object] = {}

    # -- collecting ---------------------------------------------------------

    def fail(self, field: str, message: str) -> None:
        self.errors.setdefault(field, message)

    def warn(self, message: str) -> None:
        """A note the admin should see, but which does not block the save."""
        if message not in self.warnings:
            self.warnings.append(message)

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)

    # -- readers ------------------------------------------------------------

    def text(self, field: str, label: str, required: bool = False,
             max_length: int = 120, default: str = "") -> str:
        value = str(self.form.get(field, default) or "").strip()
        if required and not value:
            self.fail(field, f"{label} is required.")
        elif len(value) > max_length:
            self.fail(field, f"{label} must be {max_length} characters or fewer.")
        self.data[field] = value
        return value

    def choice(self, field: str, label: str, allowed, required: bool = True,
               default: str = "") -> str:
        value = str(self.form.get(field, default) or "").strip()
        if required and not value:
            self.fail(field, f"{label} is required.")
        elif value and value not in allowed:
            # Either a stale page or a hand-crafted post; neither should save.
            self.fail(field, f"{label} is not a valid choice.")
        self.data[field] = value
        return value

    def multi(self, field: str, label: str, allowed, required: bool = True) -> list[str]:
        raw = (self.form.getlist(field) if hasattr(self.form, "getlist")
               else self.form.get(field) or [])
        if isinstance(raw, str):
            raw = [raw]
        values = [v for v in (str(x).strip() for x in raw) if v]
        if required and not values:
            self.fail(field, f"Select at least one {label.lower()}.")
        invalid = [v for v in values if v not in allowed]
        if invalid:
            self.fail(field, f"{label} contains an invalid choice.")
        # De-duplicate while preserving the order the admin picked.
        seen, unique = set(), []
        for v in values:
            if v not in seen:
                seen.add(v)
                unique.append(v)
        self.data[field] = unique
        return unique

    def username(self, field: str = "username", label: str = "Username") -> str:
        value = str(self.form.get(field, "") or "").strip()
        if not value:
            self.fail(field, f"{label} is required.")
        elif not USERNAME_RE.match(value):
            self.fail(field, "Username must be 3-32 characters: letters, numbers, "
                             "dot, underscore, or hyphen, starting with a letter "
                             "or number.")
        self.data[field] = value
        return value

    def phone(self, field: str = "contact_number",
              label: str = "Contact Number") -> str:
        """
        Optional PH mobile number. Stored normalised to 11 digits so that two
        records for the same person do not differ only by spacing.
        """
        raw = str(self.form.get(field, "") or "").strip()
        if not raw:
            self.data[field] = ""
            return ""
        digits = re.sub(r"[^\d]", "", raw)
        if digits.startswith("63") and len(digits) == 12:
            digits = "0" + digits[2:]
        if len(digits) != 11 or not digits.startswith("09"):
            self.fail(field, f"{label} must be an 11-digit mobile number "
                             f"starting with 09, e.g. 09175550142.")
            self.data[field] = raw
            return raw
        self.data[field] = digits
        return digits

    def time_of_day(self, field: str, label: str, required: bool = False) -> str:
        value = str(self.form.get(field, "") or "").strip()
        if not value:
            if required:
                self.fail(field, f"{label} is required.")
            self.data[field] = ""
            return ""
        if not TIME_RE.match(value):
            self.fail(field, f"{label} must be a 24-hour time, e.g. 08:30.")
        self.data[field] = value
        return value

    def date(self, field: str, label: str, required: bool = False) -> str:
        from services import timeutil
        raw = str(self.form.get(field, "") or "").strip()
        if not raw:
            if required:
                self.fail(field, f"{label} is required.")
            self.data[field] = ""
            return ""
        parsed = timeutil.to_date(raw)
        if not parsed:
            self.fail(field, f"{label} must be a valid date.")
            self.data[field] = raw
            return raw
        self.data[field] = timeutil.date_str(parsed)
        return self.data[field]
