"""Only real supplier operations used by the finite company round."""

from collections.abc import Callable
from datetime import date,datetime
from typing import Literal,Protocol

from trader.download.domain.company_annual_income import CompanyAnnualIncome,CompanyAnnualIncomeRequest
from trader.download.domain.company_evidence import (
    CompanyDocumentExtraction,Disclosure,DisclosurePage,DisclosurePartition,DisclosureQuery,DocumentRoute,
    StructuredCompanyPage,StructuredCompanyQuery,
)
from trader.download.domain.company_source_records import CompanyFinancialSourcePage


class CompanyFinancialObservationSource(Protocol):
    def financial_observations(self,query:StructuredCompanyQuery,*,observed_at:datetime)->CompanyFinancialSourcePage: ...


class CompanyRatioSource(Protocol):
    def pledge_day(self,observed_at:datetime)->date: ...
    def fetch(self,query:StructuredCompanyQuery,*,observed_at:datetime)->StructuredCompanyPage: ...


class CompanyAnnualIncomeSource(Protocol):
    def annual_income(self,request:CompanyAnnualIncomeRequest,*,observed_at:datetime)->tuple[CompanyAnnualIncome,...]: ...


class CompanyOfficialDocumentSource(Protocol):
    def fetch(self,partition:DisclosurePartition,*,order:Literal["","asc","desc"]="")->DisclosurePage: ...
    def split_queries(self,partition:DisclosurePartition)->tuple[DisclosureQuery,...]: ...
    def document_ready(self,item:Disclosure)->bool: ...
    def document_payload_hash(self,item:Disclosure)->bytes|None: ...
    def download_document(self,item:Disclosure,*,attempt_limit:int=3)->None: ...
    def extract_document(self,item:Disclosure,*,route:DocumentRoute="standard",
                         page_progress:Callable[[int,int],None]|None=None)->CompanyDocumentExtraction: ...
