from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import AnyHttpUrl, Field
from pydantic_settings import BaseSettings, SettingsConfigDict  # type: ignore


class Settings(BaseSettings):
    llm_provider: Literal['oauth', 'custom'] = 'oauth'
    llm_base_url: str = ''
    model_name: str | None = Field(None)
    embedding_model_name: str = 'BAAI/bge-m3'
    local_model_url: str = 'http://192.168.1.11:7997'
    reranker_model_name: str | None = None
    openai_state_dir: Path = Path('.openai-runtime')
    openai_callback_port: int = Field(8000, ge=1, le=65535)
    neo4j_uri: str | None = Field(None)
    neo4j_user: str | None = Field(None)
    neo4j_password: str | None = Field(None)
    falkordb_host: str | None = Field(None)
    falkordb_port: int | None = Field(None)
    falkordb_database: str | None = Field(None)
    db_backend: str = Field('neo4j')
    mcp_public_url: AnyHttpUrl | None = None
    mcp_group_id: str = 'main'

    model_config = SettingsConfigDict(env_file='.env', extra='ignore')


@lru_cache
def get_settings():
    return Settings()  # type: ignore[call-arg]


ZepEnvDep = Annotated[Settings, Depends(get_settings)]
