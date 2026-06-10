import frappe
from frappe.model.document import Document


class APSOperator(Document):
    def validate(self):
        for w in self.shifts or []:
            if w.from_time >= w.to_time:
                frappe.throw("Shift window 'From' must be before 'To'")
