from __future__ import annotations

import re
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


K8S_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{1,61}[a-z0-9])?$")


class Engine(str, Enum):
    postgresql = "postgresql"
    mysql = "mysql"
    mongodb = "mongodb"
    xtradb_cluster = "xtradb_cluster"


class Op(str, Enum):
    create = "create"
    delete = "delete"


def _validate_k8s_name(v: str) -> str:
    if not K8S_NAME_RE.match(v):
        raise ValueError("must match k8s name regex")
    return v


class OperatorsModel(BaseModel):
    postgresql: Optional[bool] = None
    mongodb: Optional[bool] = None
    xtradb_cluster: Optional[bool] = Field(default=None, alias="xtradb_cluster")


class BootstrapTenantRequest(BaseModel):
    username: str
    password: str
    namespace: str
    operators: OperatorsModel
    idempotency_key: Optional[str] = None

    _val_user = field_validator("username")(_validate_k8s_name)
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class AccountsCreate(BaseModel):
    username: str
    _val_user = field_validator("username")(_validate_k8s_name)


class AccountsSetPassword(BaseModel):
    username: str
    new_password: str
    _val_user = field_validator("username")(_validate_k8s_name)


class AccountsDelete(BaseModel):
    username: str
    _val_user = field_validator("username")(_validate_k8s_name)


class NamespacesAddRequest(BaseModel):
    namespace: str
    operators: OperatorsModel = Field(default_factory=OperatorsModel)
    take_ownership: bool = False
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class NamespacesUpdateRequest(BaseModel):
    namespace: str
    operators: OperatorsModel = Field(default_factory=OperatorsModel)
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class NamespacesRemoveRequest(BaseModel):
    namespace: str
    keep_namespace: bool = False
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class RBACAppendRequest(BaseModel):
    lines: List[str]


class RBACCanRequest(BaseModel):
    user: str
    resource: str
    verb: str
    object: str
    _val_user = field_validator("user")(_validate_k8s_name)


class LimitsUpsertRequest(BaseModel):
    namespace: str
    max_clusters: int = Field(ge=0)
    allowed_engines: List[Engine] = Field(default_factory=list)
    cpu_limit_cores: float = Field(ge=0)
    memory_limit_bytes: int = Field(ge=0)
    max_db_users: int = Field(ge=0)
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class EnforceClusterCreateRequest(BaseModel):
    namespace: str
    engine: Engine
    cpu_request_cores: float = Field(ge=0)
    memory_request_bytes: int = Field(ge=0)
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class UsageRegisterClusterRequest(BaseModel):
    namespace: str
    op: Op
    cpu_cores: float = Field(ge=0)
    memory_bytes: int = Field(ge=0)
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class UsageRegisterDbUserRequest(BaseModel):
    namespace: str
    op: Op
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class DeleteUserRequest(BaseModel):
    username: str
    namespace: Optional[str] = None
    remove_rbac: bool = False
    _val_user = field_validator("username")(_validate_k8s_name)


class DeleteNamespaceRequest(BaseModel):
    namespace: str
    force: bool = False
    _val_ns = field_validator("namespace")(_validate_k8s_name)


class RotatePasswordRequest(BaseModel):
    username: str
    new_password: str
    _val_user = field_validator("username")(_validate_k8s_name)

