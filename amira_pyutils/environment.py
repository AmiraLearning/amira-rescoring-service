from typing import Any, Callable
from enum import Enum
from dataclasses import dataclass, field, replace
import os
import yaml

from amira_pyutils.errors import MissingConfigError
from amira_pyutils.logging import get_logger

# TODO: create a more robust tracking known suffixes (s3 or some version controlled storage)
_KNOWN_PROD_SUFFIXES = ["8666", "28A5", "BF8B", "7CE9", "B6A6"]

_KNOWN_STAGE_SUFFIXES = [
    "AAAA",
    "BAAA",
    "BBBA",
    "ABBA",
    "AAAD",
    "AAAA",
    "BAAA",
    "BBBA",
    "ABBA",
    "AAAD",
    "ACCC",
    "AACC",
    "ACDC",
    "DDDA",
    "DCBA",
    "DCBB",
    "DCBC",
    "DCBD",
    "DCCA",
    "DCCB",
    "DCCC",
    "DCCD",
    "DCDA",
    "DCDB",
    "DCDC",
    "DCDD",
    "DCAA",
    "DCAB",
    "DCAC",
    "DCAD",
    "DDDB",
    "DDDC",
    "DDDD",
    "DDCA",
    "DDCB",
    "DDCC",
    "DDCD",
    "DDBD",
    "DDBC",
    "DDBB",
    "DDBA",
    "DDAD",
    "DDAC",
    "DDAB",
    "ABCD",
    "DDAA",
    "DBDD",
    "DBDC",
    "DBDB",
    "DBDA",
    "DBCD",
    "0000",
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "FF01",
    "FF02",
    "FF03",
    "FF04",
    "FF05",
    "FF06",
    "FF07",
    "FF08",
    "FF09",
    "FF10",
    "FF11",
    "FF12",
    "FF13",
    "FF14",
    "FF15",
    "FF16",
    "FF17",
    "FF18",
    "FF20",
    "FF21",
    "FF22",
    "BF01",
    "BF02",
    "BF03",
    "BF04",
    "BF05",
    "6A5B",
    "6A5C",
    "6A5D",
    "AA01",
    "FA21",
    "FF23",
    "FF24",
    "0088",
    "0089",
    "0090",
    "597F",
]

_STAGE_ALIASES = ["stage", "staging"]
_PROD_ALIASES = ["prod", "production"]
_DEFAULT_ALIASES = ["default", "from_env_vars"]


def _check_not_empty(name: str, value: Any) -> Any:
    if (value is None) or (isinstance(value, str) and len(value) == 0):
        raise MissingConfigError(config_key_name=name)
    return value


@dataclass
class Environment:
    """
    Unique values that define [access to] an environment in AWS
    """

    # Original constructor parameter order maintained for positional compatibility
    _name: str
    _aliases: list[str] = field(default_factory=list)

    _SRS_url: str = ""
    _SRS_key: str = ""

    _WordDB_SRS_url: str = ""
    _WordDB_SRS_key: str = ""

    _audio_bucket: str = ""
    _timing_bucket: str = ""

    _is_staging: bool = False
    _is_production: bool = False

    _feature_store_url: str = ""
    _feature_store_key: str = ""
    _feature_store_api_id: str = ""
    _feature_store_direct_url: str | None = None
    _feature_store_direct_key: str | None = None

    _ml_serving_uri: str = ""
    _ml_serving_batch_uri: str = ""

    _assignment_service_SRS_url: str = ""
    _assignment_service_SRS_key: str = ""

    _EPS_uri: str = ""
    _EPS_key: str = ""

    _SIS_uri: str = ""
    _SIS_key: str = ""

    _AOS_uri: str = ""
    _AOS_key: str = ""

    _transcribe_bucket: str = ""
    _pretranscribe_bucket: str = ""

    def __getattr__(self, name: str) -> Any:
        private_name = f"_{name}"
        try:
            value = object.__getattribute__(self, private_name)
        except AttributeError:
            raise AttributeError(name)
        return _check_not_empty(name, value)

    @staticmethod
    def attr_env_aliases() -> dict[str, list[str]]:
        # Mapping of attribute names to environment variable aliases
        # Multiple aliases are used in some cases to allow for backwards compatibility
        # with particular service deployments
        return {
            "SRS_url": ["SRS_URL", "STUDENT_RECORD_STORE_API"],
            "SRS_key": ["SRS_KEY", "STUDENT_RECORD_STORE_KEY"],
            "WordDB_SRS_url": ["WORDDB_SRS_URL", "WORDS_DB_HOST"],
            "WordDB_SRS_key": ["WORDDB_SRS_KEY", "WORDS_DB_KEY"],
            "audio_bucket": ["AUDIO_BUCKET"],
            "timing_bucket": ["TIMING_BUCKET"],
            "transcribe_bucket": ["TRANSCRIBE_BUCKET"],
            "pretranscribe_bucket": ["PRETRANSCRIBE_BUCKET"],
            "feature_store_url": ["FEATURE_STORE_URL"],
            "feature_store_key": ["FEATURE_STORE_KEY"],
            "feature_store_api_id": ["FEATURE_STORE_API_ID"],
            "feature_store_direct_url": ["FEATURE_STORE_DIRECT_URL"],
            "feature_store_direct_key": ["FEATURE_STORE_DIRECT_KEY"],
            "ml_serving_uri": ["ML_SERVING_URI", "AMIRA_SERVING"],
            "ml_serving_batch_uri": ["ML_SERVING_BATCH_URI", "AMIRA_SERVING"],
            "assignment_service_SRS_url": ["ASSIGNMENT_SERVICE_SRS_URL"],
            "assignment_service_SRS_key": ["ASSIGNMENT_SERVICE_SRS_KEY"],
            "EPS_uri": ["EPS_URI", "EPS_API"],
            "EPS_key": ["EPS_KEY"],
            "SIS_uri": ["SIS_URI"],
            "SIS_key": ["SIS_KEY"],
            "AOS_uri": ["AOS_URI"],
            "AOS_key": ["AOS_KEY"],
        }

    @staticmethod
    def from_dict(name: str, d: dict[str, Any]) -> "Environment":
        if name.lower() in _STAGE_ALIASES:
            is_stage = True
            is_prod = False
            aliases = _STAGE_ALIASES
        elif name.lower() in _PROD_ALIASES:
            is_stage = False
            is_prod = True
            aliases = _PROD_ALIASES
        elif name.lower() in _DEFAULT_ALIASES:
            is_stage = False
            is_prod = True
            aliases = _DEFAULT_ALIASES
        else:
            # A bespoke env can act in either capacity
            is_stage = True
            is_prod = True
            aliases = [name]

        setting_dict = {
            "_name": name,
            "_aliases": aliases,
            "_is_staging": is_stage,
            "_is_production": is_prod,
        }

        def get_val(key: str, d: dict[str, Any], required: bool = False) -> str:
            # Retrieve value from provided dictionary or environment variables
            aliases = Environment.attr_env_aliases().get(key, [])
            for idx, alias in enumerate([key] + aliases):
                if alias in d:
                    return d[alias]
                # Only the valid aliases should be seached for in ENV VARs
                if idx > 0:
                    from_env = os.getenv(alias)
                    if from_env is not None:
                        return from_env
            if required:
                raise ValueError(f"Missing required key {key} for environment {name}")
            else:
                return ""

        get_val_fields = set(Environment.__dataclass_fields__) - set(setting_dict.keys())
        for field_name in get_val_fields:
            assert field_name.startswith("_")
            setting_dict[field_name] = get_val(field_name[1:], d)

        return Environment(**setting_dict)

    def with_SRS_config(self, url: str | None, key: str | None) -> "Environment":
        return replace(self, _SRS_url=url or self.SRS_url, _SRS_key=key or self.SRS_key)

    def with_wordDB_config(self, url: str | None = None, key: str | None = None) -> "Environment":
        return replace(
            self,
            _WordDB_SRS_url=url or self.WordDB_SRS_url,
            _WordDB_SRS_key=key or self.WordDB_SRS_key,
        )

    def with_feature_store_config(self, url: str | None = None, key: str | None = None) -> "Environment":
        return replace(
            self,
            _feature_store_direct_url=url or self.feature_store_direct_url,
            _feature_store_direct_key=key or self.feature_store_direct_key,
        )

    def with_SRS_keys(
        self,
        key: str,
        wordDB_key: str,
        feature_store_key: str,
        assignment_service_key: str,
        eps_key: str,
        sis_key: str,
    ) -> "Environment":
        """This function is deprecated"""
        return replace(
            self,
            _SRS_key=key,
            _WordDB_SRS_key=wordDB_key,
            _feature_store_key=feature_store_key,
            _assignment_service_SRS_key=assignment_service_key,
            _EPS_key=eps_key,
            _SIS_key=sis_key,
        )


def create_envs_enum(name: str, members: dict[str, Environment]) -> type[Enum]:
    # This wrapping is a bit klunky but is needed to allow us to expres the Enum
    # at runtime (it depends on the environments defined in the YAML potentially)
    # and also add class methods to the Enum
    class WrappedEnvironments(Enum):
        @classmethod
        def find(cls, name: str) -> Environment | None:
            """Find an environment given its name

            :param name: Name to search for
            :return: Environment details
            """

            def find_by_property(f: Callable[[Environment], bool]) -> Environment | None:
                for env in cls:
                    if f(env.value):
                        return env.value
                return None

            # Search first by name and aliases
            result = find_by_property(
                lambda e: e.name.lower() == name.lower()
                or name.lower() in [n.lower() for n in e.aliases]
            )
            if result is None:
                # Special case for known prod/stage aliases by property (to allow a default env
                # loaded from ENV VARs only to be found)
                if name in _STAGE_ALIASES:
                    result = find_by_property(lambda e: e.is_staging)
                elif name in _PROD_ALIASES:
                    result = find_by_property(lambda e: e.is_production)
            return result

        @classmethod
        def env_from_activity(cls, activity_id: str) -> Environment | None:
            """Find the correct environment to use for a given activity id
            when inferrable.  Returns None if no inference is known

            :param activity_id:
            :return: None if no known mapping else env name to use
            """
            suffix = activity_id[-4:].upper()
            if suffix in _KNOWN_PROD_SUFFIXES:
                return cls.find("production")
            elif suffix in _KNOWN_STAGE_SUFFIXES:
                return cls.find("staging")
            else:
                return None

    return WrappedEnvironments(name, members)


# Attempt to load the configs from a static file if present
_DEFAULT_ENV_CONFIG_FILE = "amira_pyutils/environments.template.yaml"
_CONFIG_FILE_OVERRIDE_ENV_VAR = "AMIRA_ENV_CONFIG_FILE"

logger = get_logger(__name__)


def construct_envs() -> dict[str, Environment]:
    # Check for a local config file first to provide everything in bulk
    config_filename = os.environ.get(_CONFIG_FILE_OVERRIDE_ENV_VAR, _DEFAULT_ENV_CONFIG_FILE)
    logger.info(f"Loading environment configs from {config_filename}")
    try:
        with open(config_filename, "r") as f:
            env_configs = yaml.safe_load(f)
            envs = {
                "DEFAULT": Environment.from_dict("DEFAULT", {}),
                **{
                    name.upper(): Environment.from_dict(name, data)
                    for name, data in env_configs.items()
                },
            }
    except FileNotFoundError:
        # If no config YAML found then assume that individual properties required are set
        # in ENV VARs and construct the environments from those.  Since this may apply to a
        # deployment in either PROD or STAGE we need to create both
        envs = {
            "STAGING": Environment.from_dict("STAGING", {}),
            "PRODUCTION": Environment.from_dict("PRODUCTION", {}),
            "DEFAULT": Environment.from_dict("DEFAULT", {}),
        }
    return envs


# Resulting Enum should be backward compatible with the static one we used to have,
# but with values now loaded from external sources at runtime
Environments = create_envs_enum("Environments", construct_envs())
