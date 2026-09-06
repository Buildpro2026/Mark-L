

## PURPOSE

This document defines the external systems JARVIS is expected to use to operate BuildPro Recruiters LLC.

A system listed here does NOT mean JARVIS currently has access to it.

JARVIS must distinguish between:

- PLANNED
- CONNECTED
- AUTHENTICATED
- FUNCTIONAL
- ACTION-CAPABLE
- VERIFIED
- BLOCKED
- NOT AVAILABLE

JARVIS must never claim an integration works merely because code for it exists.

---

# CORE BUSINESS SYSTEMS

## Gmail / Business Email

Purpose:
- Read approved business email
- Identify leads
- Identify candidate communications
- Identify employer communications
- Draft responses
- Send approved communications
- Monitor important messages
- Extract actionable tasks

Required capabilities:
- Read
- Search
- Draft
- Send
- Label/organize where authorized

Status:
UNKNOWN — must be verified against the actual JARVIS implementation.

---

## Google Calendar

Purpose:
- Read calendar
- Identify meetings
- Schedule meetings
- Detect conflicts
- Prepare daily schedule
- Track follow-ups
- Create reminders

Required capabilities:
- Read
- Create
- Modify
- Delete only with appropriate authority

Status:
UNKNOWN — must be verified.

---

## Airtable

Purpose:
- Candidate database
- Employer database
- Recruiting pipeline
- Business operations
- Reporting
- Structured company data

Required capabilities:
- Read
- Search
- Create
- Update
- Archive/delete only when authorized

Status:
UNKNOWN — must be verified.

---

## HubSpot

Purpose:
- Employer/client CRM
- Prospects
- Companies
- Contacts
- Deals
- Follow-up tracking
- Sales pipeline

Required capabilities:
- Read
- Search
- Create contacts
- Create companies
- Update records
- Create tasks
- Track pipeline
- Log interactions

Status:
UNKNOWN — must be verified.

---

## LinkedIn

Purpose:
- Market intelligence
- Employer research
- Candidate research
- Recruiting research
- Company intelligence

IMPORTANT:

JARVIS must follow LinkedIn's terms and applicable platform restrictions.

Do not automate prohibited login, scraping, messaging, or engagement behavior.

JARVIS may use permitted public information and approved integrations/tools.

Status:
UNKNOWN — must be verified.

---

## Indeed / Job Boards

Purpose:
- Job market research
- Employer research
- Job intelligence
- Candidate/job matching

JARVIS must not claim access to private job-board data unless a legitimate authorized integration exists.

Status:
UNKNOWN — must be verified.

---

## Apollo

Purpose:
- Business intelligence
- Employer/company research
- Contact discovery
- Prospect research
- Sales intelligence

Required capabilities depend on actual account permissions and available integration.

Status:
UNKNOWN — must be verified.

---

## Social Media

Potential systems:

- LinkedIn
- Facebook
- Instagram
- TikTok
- Other approved channels

Purpose:
- Brand awareness
- Recruiting marketing
- Employer acquisition
- Candidate acquisition
- Content distribution

JARVIS should:

RESEARCH
→ DRAFT
→ PRESENT FOR APPROVAL

until publishing authority is explicitly delegated.

Status:
UNKNOWN — verify individually.

---

## Cloud Storage

Potential systems:

- Google Drive
- Dropbox
- Other approved storage

Purpose:
- Resumes
- Candidate documents
- Employer documents
- Contracts
- Marketing assets
- Company files
- Research

Required capabilities:
- Search
- Read
- Organize
- Create
- Update

Deletion requires appropriate authority.

Status:
UNKNOWN — must be verified.

---

# WEBSITE

## BuildPro Recruiters Website

Purpose:
- Employer acquisition
- Candidate acquisition
- Lead generation
- Brand authority
- Resume collection
- Content marketing

JARVIS should eventually be capable of:

- Monitor site health
- Monitor forms
- Monitor leads
- Analyze conversion
- Identify UX problems
- Recommend content improvements
- Recommend SEO improvements
- Monitor analytics where authorized
- Prepare website changes
- Deploy changes only when explicitly authorized

Status:
PARTIALLY VERIFIED — actual capabilities must be audited.

---

# RECRUITING DATA

JARVIS should eventually operate around:

## Employers

- Company
- Industry
- Location
- Contact
- Open roles
- Hiring activity
- Relationship
- Pipeline status

## Candidates

- Name
- Contact
- Discipline
- Location
- Experience
- Skills
- Resume
- Availability
- Compensation expectations
- Match history
- Communication history

## Jobs

- Employer
- Position
- Location
- Compensation
- Requirements
- Status
- Candidate matches
- Submission status

---

# INTEGRATION REALITY RULE

For every integration JARVIS should report:

### ACCESS

Does the credential/token exist?

### AUTHENTICATION

Does authentication succeed?

### CONNECTIVITY

Can JARVIS communicate with the service?

### READ

Can JARVIS retrieve information?

### WRITE

Can JARVIS create/update information?

### EXECUTION

Can JARVIS perform the intended business action?

### VERIFICATION

Can JARVIS confirm the action actually happened?

---

# CAPABILITY MATRIX

JARVIS should maintain a current matrix:

| System | Connected | Read | Write | Execute | Verify | Status |
|---|---|---|---|---|---|---|
| Gmail | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Calendar | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Airtable | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| HubSpot | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Apollo | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| LinkedIn | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Job Boards | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Social Media | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Google Drive | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| Website | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |

This table must be updated only from verified evidence.

---

# MISSING CAPABILITY PROTOCOL

If JARVIS needs to perform an action but cannot:

1. Identify the missing capability.
2. Identify the required integration.
3. Identify required permissions.
4. Identify whether a safe workaround exists.
5. Recommend the shortest path to enable it.
6. Do not pretend the action occurred.

---

# INTEGRATION PRIORITY

Prioritize integrations based on business value.

Initial priority:

1. Gmail
2. Google Calendar
3. Airtable
4. HubSpot
5. Apollo
6. Website
7. Cloud Storage
8. Approved recruiting/job sources
9. Social media
10. Other systems

This order may change based on actual business requirements and revenue opportunity.

---

# SECURITY

Never store:

- API keys
- Passwords
- OAuth tokens
- Session cookies
- Private credentials

in this document.

Store only connection metadata and secure credential locations.

---

# OPERATING PRINCIPLE

A tool is valuable only when JARVIS can actually use it.

Therefore:

CODE EXISTS ≠ INTEGRATION WORKS

INTEGRATION EXISTS ≠ AUTHENTICATION WORKS

AUTHENTICATION WORKS ≠ ACTION WORKS

ACTION WORKS ≠ ACTION VERIFIED

JARVIS must always report the actual capability level.