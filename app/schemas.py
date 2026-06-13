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


class ExpenseCreate(ExpenseBase):
    pass


class ExpenseUpdate(BaseModel):
    """Partial update — used when the user labels an unknown merchant."""
    merchant: str | None = None
    category: str | None = None
    is_labeled: bool | None = None


class ExpenseOut(ExpenseBase):
    model_config = ConfigDict(from_attributes=True)
    id: int


class BudgetIn(BaseModel):
    amount: float = Field(gt=0)


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    category: str  # a category name, or "overall" for the whole month
    amount: float
