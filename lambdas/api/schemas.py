from pydantic import BaseModel, ConfigDict, Field

MAX_CATEGORY = 120


class KeysRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keys: list[str] = Field(min_length=1)


class CategoryRequest(KeysRequest):
    category: str = Field(min_length=1, max_length=MAX_CATEGORY)


class RenameRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # "from" is a keyword, so the wire name and the attribute differ.
    source: str = Field(alias="from", min_length=1)
    target: str = Field(alias="to", min_length=1, max_length=MAX_CATEGORY)


class FavouriteRequest(KeysRequest):
    on: bool = True


class HideRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    categories: list[str] = Field(min_length=1)
    on: bool = True
