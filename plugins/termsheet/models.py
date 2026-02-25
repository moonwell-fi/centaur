"""Data models for term sheets and deals."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DealStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"


class InstrumentType(str, Enum):
    SAFE = "safe"
    PRICED = "priced"
    CONVERTIBLE_NOTE = "convertible_note"


class BoardRights(str, Enum):
    SEAT = "seat"
    OBSERVER = "observer"
    SEAT_AND_OBSERVER = "seat_and_observer"
    NONE = "none"


@dataclass
class TokenRights:
    enabled: bool = False
    side_letter: bool = False
    warrant: bool = False
    pro_rata_on_tokens: bool = False
    token_floor_percent: float = 50.0


@dataclass
class TermSheet:
    company_name: str
    investment_amount: float
    instrument_type: InstrumentType

    valuation_cap: float | None = None
    discount_percent: float | None = None

    pre_money_valuation: float | None = None
    post_money_valuation: float | None = None
    series: str | None = None

    option_pool_percent: float = 10.0
    option_pool_timing: str = "post"

    board_rights: BoardRights = BoardRights.OBSERVER
    pro_rata_rights: bool = True
    token_rights: TokenRights = field(default_factory=TokenRights)

    legal_fee_cap: float = 75000.0
    exclusivity_days: int = 45
    governing_law: str = "Delaware"

    liquidation_preference: str = "1x non-participating"
    anti_dilution: str = "broad-based weighted average"

    custom_terms: str = ""

    founder_name: str = ""
    dri_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_name": self.company_name,
            "investment_amount": self.investment_amount,
            "instrument_type": self.instrument_type.value,
            "valuation_cap": self.valuation_cap,
            "discount_percent": self.discount_percent,
            "pre_money_valuation": self.pre_money_valuation,
            "post_money_valuation": self.post_money_valuation,
            "series": self.series,
            "option_pool_percent": self.option_pool_percent,
            "option_pool_timing": self.option_pool_timing,
            "board_rights": self.board_rights.value,
            "pro_rata_rights": self.pro_rata_rights,
            "token_rights": {
                "enabled": self.token_rights.enabled,
                "side_letter": self.token_rights.side_letter,
                "warrant": self.token_rights.warrant,
                "pro_rata_on_tokens": self.token_rights.pro_rata_on_tokens,
                "token_floor_percent": self.token_rights.token_floor_percent,
            },
            "legal_fee_cap": self.legal_fee_cap,
            "exclusivity_days": self.exclusivity_days,
            "governing_law": self.governing_law,
            "liquidation_preference": self.liquidation_preference,
            "anti_dilution": self.anti_dilution,
            "custom_terms": self.custom_terms,
            "founder_name": self.founder_name,
            "dri_name": self.dri_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TermSheet":
        token_data = data.get("token_rights", {})
        token_rights = TokenRights(
            enabled=token_data.get("enabled", False),
            side_letter=token_data.get("side_letter", False),
            warrant=token_data.get("warrant", False),
            pro_rata_on_tokens=token_data.get("pro_rata_on_tokens", False),
            token_floor_percent=token_data.get("token_floor_percent", 50.0),
        )

        return cls(
            company_name=data["company_name"],
            investment_amount=data["investment_amount"],
            instrument_type=InstrumentType(data["instrument_type"]),
            valuation_cap=data.get("valuation_cap"),
            discount_percent=data.get("discount_percent"),
            pre_money_valuation=data.get("pre_money_valuation"),
            post_money_valuation=data.get("post_money_valuation"),
            series=data.get("series"),
            option_pool_percent=data.get("option_pool_percent", 10.0),
            option_pool_timing=data.get("option_pool_timing", "post"),
            board_rights=BoardRights(data.get("board_rights", "observer")),
            pro_rata_rights=data.get("pro_rata_rights", True),
            token_rights=token_rights,
            legal_fee_cap=data.get("legal_fee_cap", 75000.0),
            exclusivity_days=data.get("exclusivity_days", 45),
            governing_law=data.get("governing_law", "Delaware"),
            liquidation_preference=data.get("liquidation_preference", "1x non-participating"),
            anti_dilution=data.get("anti_dilution", "broad-based weighted average"),
            custom_terms=data.get("custom_terms", ""),
            founder_name=data.get("founder_name", ""),
            dri_name=data.get("dri_name", ""),
        )


@dataclass
class Deal:
    id: str
    company_name: str
    status: DealStatus
    term_sheet: TermSheet
    requester_user_id: str
    requester_user_name: str
    slack_channel: str
    slack_thread_ts: str
    created_at: str
    updated_at: str
    approved_at: str | None = None
    approved_by: str | None = None
    sent_at: str | None = None
    revision_history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "company_name": self.company_name,
            "status": self.status.value,
            "term_sheet": self.term_sheet.to_dict(),
            "requester_user_id": self.requester_user_id,
            "requester_user_name": self.requester_user_name,
            "slack_channel": self.slack_channel,
            "slack_thread_ts": self.slack_thread_ts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "sent_at": self.sent_at,
            "revision_history": self.revision_history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Deal":
        return cls(
            id=data["id"],
            company_name=data["company_name"],
            status=DealStatus(data["status"]),
            term_sheet=TermSheet.from_dict(data["term_sheet"]),
            requester_user_id=data["requester_user_id"],
            requester_user_name=data["requester_user_name"],
            slack_channel=data["slack_channel"],
            slack_thread_ts=data["slack_thread_ts"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            approved_at=data.get("approved_at"),
            approved_by=data.get("approved_by"),
            sent_at=data.get("sent_at"),
            revision_history=data.get("revision_history", []),
        )
