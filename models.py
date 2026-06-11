from pydantic import BaseModel, Field
from typing import Optional, List
from enum import IntEnum


class AuthorityRating(IntEnum):
    EXECUTIVE = 1       # CEO, Founder, MD, Owner, Partner
    VP_DIRECTOR = 2     # VP HR, HR Director, Chief People Officer
    MANAGER = 3         # HR Manager, TA Manager, Head of Recruiting
    SPECIALIST = 4      # Recruiter, HR Business Partner, TA Specialist
    SUPPORT = 5         # HR Coordinator, HR Assistant, Other


class PhoneDetail(BaseModel):
    """Structured phone number with validation metadata."""
    raw: str
    e164: Optional[str] = None                  # +442079460958
    international: Optional[str] = None         # +44 20 7946 0958
    national: Optional[str] = None              # 020 7946 0958
    country_code: Optional[str] = None          # GB
    number_type: str = "unknown"                # mobile | fixed_line | voip | toll_free
    carrier_name: Optional[str] = None
    valid: bool = False
    source: str = ""
    confidence: int = 0


class CompanyContactInfo(BaseModel):
    """Company-level contact data — not tied to any individual person."""
    phone: Optional[str] = None               # main number (E.164)
    phone_detail: Optional[PhoneDetail] = None
    email: Optional[str] = None               # best generic contact email (info@, kontakt@)
    website: Optional[str] = None
    address: Optional[str] = None


class Contact(BaseModel):
    full_name: str
    title: Optional[str] = None
    company: str
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    phone: Optional[str] = None              # direct line only (null if only company number known)
    phone_detail: Optional[PhoneDetail] = None
    direct_phone: Optional[str] = None
    direct_phone_detail: Optional[PhoneDetail] = None
    linkedin_url: Optional[str] = None
    source: str
    rating: int = Field(ge=1, le=5)
    rating_reason: str
    confidence: int = Field(default=0, ge=0, le=100)   # 0-100: how confident person currently works here
    employment_confirmed: bool = False                  # confirmed via LinkedIn/Xing/company page/register
    data_year: Optional[int] = None
    recency_note: Optional[str] = None


class EnrichmentRequest(BaseModel):
    company_name: str
    location: str = ""
    job_category: str = ""
    domain: Optional[str] = None
    callback_url: Optional[str] = None
    max_contacts: int = Field(default=5, ge=1, le=50)
    find_direct_lines: bool = False


class EnrichmentResult(BaseModel):
    company_name: str
    domain: Optional[str] = None
    company_contact_info: Optional[CompanyContactInfo] = None
    contacts: List[Contact] = []
    total_found: int = 0
    sources_used: List[str] = []
    errors: List[str] = []
    status: str = "completed"
