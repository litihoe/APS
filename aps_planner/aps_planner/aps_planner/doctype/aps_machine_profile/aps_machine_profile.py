import frappe
from frappe.model.document import Document


class APSMachineProfile(Document):
    def validate(self):
        for field in ("performance_factor", "availability_derate", "quality_yield"):
            value = self.get(field)
            if value is not None and not (0 < value <= 1.5):
                frappe.throw(
                    f"{self.meta.get_label(field)} must be in (0, 1.5], got {value}"
                )
        for w in self.maintenance_windows or []:
            if w.from_time >= w.to_time:
                frappe.throw("Maintenance window 'From' must be before 'To'")
