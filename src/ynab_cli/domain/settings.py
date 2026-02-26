from attrs import define, field


@define
class YnabSettings:
    access_token: str = ""
    budget_id: str = ""


@define
class Settings:
    ynab: YnabSettings = field(factory=YnabSettings)
    output_format: str = "table"
    show_ids: bool = False
