"""Allowed values for the study start-up domain (section 5 of the brief).

The generator uses these to create data; the MCP server uses them to
validate writes.
"""

SITE_STATUSES = [
    "Identified",
    "Selected",
    "Docs In Progress",
    "Contract In Review",
    "Ready To Activate",
    "Activated",
    "On Hold",
]

# In the order they normally happen.
MILESTONE_TYPES = [
    "Feasibility Complete",
    "CDA Signed",
    "Ethics Submission",
    "Ethics Approval",
    "Contract Signed",
    "Site Initiation Visit",
    "Activation",
]
MILESTONE_STATUSES = ["Pending", "Completed", "Delayed"]

DOC_TYPES = [
    "Ethics/IRB approval",
    "Principal Investigator CV",
    "GCP certificate",
    "Financial disclosure",
    "Clinical trial agreement",
    "Site budget",
    "Lab certification",
]
DOC_STATUSES = ["Not Started", "Submitted", "Approved", "Rejected", "Expired"]

# Approved documents of these types carry an expiry date.
EXPIRING_DOC_TYPES = [
    "Ethics/IRB approval",
    "GCP certificate",
    "Financial disclosure",
    "Lab certification",
]

STAFF_ROLES = [
    "Principal Investigator",
    "Sub-Investigator",
    "Study Coordinator",
    "Research Nurse",
    "Pharmacist",
]

THERAPEUTIC_AREAS = ["Oncology", "Cardiology", "Neurology", "Immunology", "Respiratory"]
