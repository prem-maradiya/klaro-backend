from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field


class ExpenseBase(BaseModel):
    merchant: str
    amount: float
    date: datetime
    raw_sms: str = ""
    upi_id: str | None = None
    is_labeled: bool = True
    category: str | None = None
    method: str = "upi"  # upi | card
    direction: str = "debit"  # debit = money out, credit = money in (income)
    # How it entered Klaro: manual | sms | email | upi | aa. Optional on create
    # (the server infers it from raw_sms for older app builds); always returned.
    source: str | None = None
    # Budget scoping (returned to clients). Whether this spend counts toward the
    # monthly budget, and whether it's a large/fee-like capture awaiting the user's
    # include/exclude decision. On create these are ignored — the server decides via
    # the ingestion heuristic; the client sets them later through PATCH.
    in_budget: bool = True
    budget_review: bool = False


class ExpenseCreate(ExpenseBase):
    pass


class ExpenseUpdate(BaseModel):
    """Partial update — labeling an unknown merchant, or deciding a budget review
    (set in_budget to the user's choice and clear budget_review)."""
    merchant: str | None = None
    category: str | None = None
    is_labeled: bool | None = None
    in_budget: bool | None = None
    budget_review: bool | None = None


class ExpenseOut(ExpenseBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class BudgetIn(BaseModel):
    amount: float = Field(gt=0)


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    category: str  # a category name, or "overall" for the whole month
    amount: float
