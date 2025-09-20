"""Module for various AppSync-related data operations.

Includes a couple of core interfaces for Appsync, and many specialized functions.
"""

import json
import math
import random
import re
import uuid
from collections.abc import Callable, Iterable
from copy import copy
from datetime import datetime
from time import sleep, time
from typing import Any, Final, cast

import boto3
import botocore.exceptions
import requests
from requests.adapters import HTTPAdapter
from toolz import curry
from urllib3.util.retry import Retry

from amira_pyutils.environment import Environment
from amira_pyutils.errors import AppsyncError
from amira_pyutils.functional import const, fold_opt
from amira_pyutils.logging import get_logger

try:
    import numpy as np
    import pandas as pd
    from tqdm import tqdm
except ImportError:
    pass

logger = get_logger(__name__)

try:
    _default_appsync_client = boto3.client("appsync")
except botocore.exceptions.NoRegionError:
    _default_appsync_client = boto3.client("appsync", region_name="us-east-1")

BotoClient = Any

RETRYABLE_ERROR_MESSAGE_FRAGMENTS: Final[list[str]] = [
    "provisioned throughput",
    "DependencyFailedException",
]

GET_ACTIVITY_DEFAULT_FIELDS: Final[list[str]] = [
    "activityId",
    "type",
    "status",
    "displayStatus",
    "errors",
    "storyId",
    "studentId",
    "story.chapters.phrases",
]

GET_ACTIVITY_PHRASE_FLAGS_FIELDS: Final[list[str]] = [
    "activityId",
    "type",
    "status",
    "displayStatus",
    "storyId",
    "studentId",
    "flagging.triggered",
]

GET_STORY_CACHE_DEFAULT_FIELDS: Final[list[str]] = [
    "chapters.phrases",
    "chapters.items.type",
    "grade",
]

DEFAULT_ASSIGNMENT_ACTIVITY_FIELDS: Final[list[str]] = ["activityId", "type", "status", "tags"]

SLASH_N: Final[str] = "\n"
TIME_FORMATTER: Final[str] = "%Y-%m-%dT%H:%M:%S.%fZ"
MAX_ACTIVITIES_BATCH_SIZE: Final[int] = 100

STACK_SESSION_TIMEOUT: Final[int] = 5
STACK_REQUEST_TIMEOUT: Final[int] = 5
NONSTACK_SESSION_TIMEOUT: Final[int] = 30
NONSTACK_REQUEST_TIMEOUT: Final[int] = 30
NONSTACK_RATE_THROTTLE: Final[float] = 1.0

GRADE_STR_TO_CHAR: Final[dict[str, str]] = {
    "KINDERGARDEN": "K",
    "KINDERGARTEN": "K",
    "FIRST GRADE": "1",
    "SECOND GRADE": "2",
    "THIRD GRADE": "3",
    "FOURTH GRADE": "4",
    "FIFTH GRADE": "5",
}

WORDDB_FIELD_NORMING: Final[dict[str, type]] = {
    "ngram": float,
    "NLET": int,
    "NSYL": int,
    "AREA_AOA": float,
    "NPHON": int,
    "PHON": str,
}

# GraphQL Queries
EXTRACT_FEATURES_QUERY: Final[str] = """
query extractFeatures($activityId: String!) {
  getModelFeaturesByActivityId(activityId: $activityId) {
    AREA_AOA
    AREA_discrim
    ASR6_AllIn
    ASR6_Count
    ASR8_AllIn
    ASR8_Count
    ASR_rollup
    Adj_Frame
    Adj_Frame_Fixed_AREA_AOA
    Adj_Frame_Scaled
    Allin_cl
    Amazon_Exp
    Amazon_Frame
    Amazon_Frame_Scaled
    Amazon_MetaFlipped
    Amazon_Meta_Rec_Word
    Amazon_Meta_Score
    Amazon_Meta_Score_Z
    Amazon_Rec_Word
    Amazon_Score
    Amazon_Score_File_ID_blank_count
    Amazon_Score_Scaled
    Amazon_Score_Z
    Amazon_blank_Exp
    Amazon_blank_neg
    Amazon_blank_pos
    Amazon_blank_pos_pred
    Amazon_fuzzy_blank
    Amazon_match
    AoA_from_Z
    Blank_ASR_count
    Blank_Words_v1
    Blank_Words_v2
    createdAt
    Error_Run
    Expected
    FAM
    Fixed_AREA_AOA
    Fixed_AREA_discrim
    Fixed_Frame
    Fixed_Frame_Fixed_AREA_AOA
    Fixed_Frame_Scaled
    Fixed_SIGHT
    Fixed_log_ngram_count_Frame_SYL
    Fixed_log_ngram_counts
    Fixed_ngram
    Frame_SYL
    Google_Exp
    Google_MetaFlipped
    Google_Meta_Rec_Word
    Google_Meta_Score
    Google_Meta_Score_Z
    Google_Rec_Word
    Google_Score
    Google_Score_File_ID_blank_count
    Google_Score_Scaled
    Google_Score_Z
    Google_blank_Exp
    Google_blank_neg
    Google_blank_pos
    Google_blank_pos_pred
    Google_fuzzy_blank
    Google_match
    Grade_level
    K_F_NCATS
    K_F_NSAMP
    KaldiNa_Duration
    KaldiNa_End_Time
    KaldiNa_Exp
    KaldiNa_Frame
    KaldiNa_Frame_Scaled
    KaldiNa_Lapse
    KaldiNa_Lapse_Scaled
    KaldiNa_Rec_Word
    KaldiNa_Score
    KaldiNa_Score_Scaled
    KaldiNa_Score_Z
    KaldiNa_Start_Time
    KaldiNa_blank_Exp
    KaldiNa_blank_neg
    KaldiNa_blank_pos
    kaldi_blank_exp_v2
    kaldiNa_blank_exp_v2
    KaldiNa_match
    Kaldi_Duration
    Kaldi_End_Time
    Kaldi_Exp
    Kaldi_Frame
    Kaldi_Frame_Scaled
    Kaldi_Lapse
    Kaldi_Lapse_Scaled
    Kaldi_MetaFlipped
    Kaldi_Meta_Rec_Word
    Kaldi_Meta_Score
    Kaldi_Meta_Score_Z
    Kaldi_Rec_Word
    Kaldi_Score
    Kaldi_Score_File_ID_blank_count
    Kaldi_Score_Scaled
    Kaldi_Score_Z
    Kaldi_Start_Time
    Kaldi_blank_Exp
    Kaldi_blank_neg
    Kaldi_blank_pos
    Kaldi_blank_pos_pred
    Kaldi_fuzzy_blank
    Kaldi_match
    kaldis_agree
    kaldi_google_agree
    kaldi_f1
    kaldina_f1
    google_f1
    Low_Conf_Words_n1
    Low_Conf_Words_n5
    Low_Conf_Words_n7
    Low_Conf_Words_p1
    Low_Syllable_Skip
    Meta_ASR
    NLET
    NSYL
    Negative_ASR_count
    PhonAware_Fixed_AREA_AOA
    PhonAware_ngram
    Poly_Target
    Positive_ASR_count
    Rec_Words_Match_p60
    Rec_Words_Match_p9
    Rec_Words_Match_p90
    SIGHT
    Sch_AoA_Kup
    Score_Target
    Target
    Utterance
    Utterance_fuzzy_blank
    WCPM_Fixed_AREA_AOA
    WCPM_TNdivTP
    WCPM_allDiv_TNFP_TPFN
    WCPM_allDiv_TNTP
    WCPM_ngram
    WORDERR_NORM
    WPCM_TNFPdivTPFN
    WPM
    WPM_TNFPdivTPFN
    WPM_TNdivTP
    Word
    Word_Pos
    activityId
    activity_error_rate
    ae_dist
    ag_dist
    agree_index
    ak_dist
    all_z_fam
    amazon_alt
    amazon_confidence
    amazon_expected
    amazon_null
    amazon_other_word
    areaMaxScore
    areaScore
    asr_count_blanks_pos
    asr_diff
    asr_dist
    asr_exp
    asr_quality_score
    avg_wright
    avg_wright_kid
    bayes_factor
    best_time
    best_time_Fixed_ngram
    best_time_Fixed_ngram_student_skill
    blank_prob_lr_amz_ggl_score
    blank_prob_lr_asr_blank_cutoff
    blank_prob_nbc_asr_blank_cutoff
    comprehensionScore
    correct_confidences
    correct_word_endings
    decodeScore
    dyslexiaRanTime
    dyslexiaRiskIndicator
    end_ed
    end_s
    end_start_Fixed_AREA_AOA
    error_prediction
    esriScore
    ge_dist
    google_alt
    google_confidence
    google_expected
    google_null
    google_other_word
    has_past_tense
    has_plural
    is_name
    is_past_tense
    is_plural
    is_short_word
    kaldiNa_confidence
    kaldi_alt
    kaldi_confidence
    kaldi_expected
    kaldi_null
    kaldi_other_word
    ke_dist
    kg_dist
    kg_score
    log_bayes_factor
    log_ngram_counts
    manual_threshold_lr_asr_blank_cutoff
    manual_threshold_nbc_asr_blank_cutoff
    model
    nae_dist
    nag_dist
    nag_score
    nak_dist
    nak_score
    ngram
    no_blank_prob_lr_amz_ggl_score
    no_blank_prob_lr_asr_blank_cutoff
    no_blank_prob_nbc_asr_blank_cutoff
    numStoryWords
    numWordsRead
    nwfRawScore
    nwfScore
    pctRead
    phonAwareScore
    phraseIndex
    phraseIndexFeat
    phrase_error_rate
    phrase_index
    pred_lr_amz_ggl_score
    ratio_kid_right
    spellingRawScore
    spellingScore
    storyId
    studentId
    student_relative_Amazon_Score
    student_relative_Google_Score
    student_relative_Kaldi_Frame
    student_relative_Kaldi_Lapse
    student_relative_Kaldi_Score
    student_skill
    student_word_error_rate
    timeRead
    timestamp
    vocabSize
    wcpmScore
    wcpm_avg_wright
    wer
    word_index
    worderr_studenterr_adj
    worderr_stuerr
    wright_Amazon
    wright_Amazon_kid_right
    wright_Amazon_kid_wrong
    wright_Google
    wright_Google_kid_right
    wright_Google_kid_wrong
    wright_Kaldi
    wright_Kaldi_kid_right
    wright_Kaldi_kid_wrong
  }
}
"""

GET_EXPECTED_TEXT_QUERY: Final[str] = """
query getExpectedText($activityId: [String]!) {
  getActivity(activityId: $activityId) {
    activityId
    type
    story {
      grade
      chapters {
        phrases
      }
    }
  }
}
"""

GET_ACTIVITIES_STUDENT_IDS_QUERY: Final[str] = """
query Q($activityId: [String]!) {
  getActivity(activityId: $activityId) {
    activityId
    studentId
  }
}
"""

GET_YELLOW_FLAG_FRACTION_QUERY: Final[str] = """
query checkActivity($activityId: [String]!) {
getActivity(activityId: $activityId) {
    activityId
    flagging{
    triggered
    }
}
}
"""

GET_ACTIVITIES_STATUS_QUERY: Final[str] = """
query checkActivity($activityId: [String]!) {
  getActivity(activityId: $activityId) {
    activityId
    status
    score_pass
  }
}
"""

GET_ASSIGNMENT_ACTIVITIES_QUERY: Final[str] = """
query Assigns($assignmentId: [String]!) {{
  assignmentActivities(assignmentId:$assignmentId) {{
    assignmentId
    activities {{
        {fields}
    }}
  }}
}}
"""

GET_ACTIVITIES_NOISE_DETECTOR_QUERY: Final[str] = """
query Q($activityId: [String]!) {
  getActivity(activityId: $activityId) {
    activityId
    flagging {
      detectors {
        name
        triggered
      }
    }
  }
}
"""

GET_STUDENT_GRADE_QUERY: Final[str] = """
query get_kid {{
    student(user_id:"{studentId}") {{
        grade
    }}
}}
"""

GET_AOS_PRIORS_QUERY: Final[str] = """
query getPriors {{
    getDefaultThetas {{
        grade
        mean
    }}
}}
"""

SET_MODEL_FEATURES_MUTATION: Final[str] = """
mutation SetFeatures ($studentId: String!, $createdAt: Int!, $input: ModelFeaturesInput!) {
  setModelFeatures(studentId: $studentId, createdAt: $createdAt, input: $input) {
    createdAt
  }
}
"""

_nonstack_rate_throttle = NONSTACK_RATE_THROTTLE


def _get_default_client() -> BotoClient:
    """Get the default AppSync client."""
    return _default_appsync_client


def _norm_field(*, name: str, value: Any) -> Any:
    """Normalize field values based on WordDB field types.

    Args:
        name: Field name to normalize.
        value: Value to normalize.

    Returns:
        Normalized value or original value if no normalization needed.
    """
    if value is not None:
        return fold_opt(const(value), lambda f: f(value), WORDDB_FIELD_NORMING.get(name))
    else:
        return value


def set_non_stack_rate_throttle(*, value: float) -> None:
    """Set the non-stack rate throttle value.

    Args:
        value: New rate throttle value.
    """
    global _nonstack_rate_throttle
    _nonstack_rate_throttle = value


def appsync_timeout_kwargs(*, stack_deployment: bool) -> dict[str, Any]:
    """Get timeout configuration based on deployment type.

    Args:
        stack_deployment: Whether this is a stack deployment.

    Returns:
        Dictionary of timeout configuration parameters.
    """
    if stack_deployment:
        return {"session_timeout": STACK_SESSION_TIMEOUT, "request_timeout": STACK_REQUEST_TIMEOUT}
    else:
        return {
            "session_timeout": NONSTACK_SESSION_TIMEOUT,
            "request_timeout": NONSTACK_REQUEST_TIMEOUT,
            "rate_throttle": _nonstack_rate_throttle,
        }


def _to_fields_clause(*, fields: dict[str, Any]) -> str:
    """Convert fields dictionary to GraphQL clause format.

    Args:
        fields: Dictionary of field names and values.

    Returns:
        GraphQL-formatted fields clause.
    """

    def to_json(k: str, v: Any) -> str:
        if k == "tags":
            if isinstance(v, list):
                return f"[{','.join(v)}]"
            else:
                return f"[{v}]"
        else:
            return json.dumps(v)

    all_fields = [f"{name}: {to_json(name, value)}" for name, value in fields.items()]
    return ",".join(all_fields)


class ActivitySpecifier:
    """Specification for an activity type and locale."""

    def __init__(self, *, activity_type: str, locale: str) -> None:
        """Initialize activity specifier.

        Args:
            activity_type: Type of activity.
            locale: Locale for the activity.
        """
        self.activity_type = activity_type
        self.locale = locale


class AppSyncSchema:
    """Schema constants for AppSync operations."""

    ACTIVITY_ID: Final[str] = "activityId"
    TRUNCATED_ID: Final[str] = "truncatedId"
    PHRASE_IDX: Final[str] = "phraseIndex"
    WORD_IDX: Final[str] = "word_index"
    EXPECTED_TEXT: Final[str] = "expected_text"
    CREATED_AT: Final[str] = "createdAt"
    MODEL: Final[str] = "model"
    PHRASES: Final[str] = "phrases"
    ACTIVITY_TYPE: Final[str] = "activity_type"
    STORY_GRADE: Final[str] = "story_grade"
    YELLOW_PHRASE_FRACTION: Final[str] = "yellow_phrase_fraction"
    YELLOW_PHRASE: Final[str] = "yellow_phrase"
    TYPE_TUTOR: Final[str] = "tutor"
    TYPE_ASSESSMENT: Final[str] = "assessment"


class StoreVariant:
    """Store variant constants."""

    DEFAULT: Final[int] = 0
    WORD_DB: Final[int] = 1
    FEATURE_STORE: Final[int] = 2
    ASSIGNMENT_SERVICE: Final[int] = 3
    SIS: Final[int] = 4
    AOS: Final[int] = 5
    BESPOKE: Final[int] = 6


class AppSync:
    """Generic interface for AppSync for queries and idempotent mutations."""

    def __init__(
        self,
        *,
        env: Environment,
        session_timeout: float = 30,
        request_timeout: float = 30,
        max_retries: int = 5,
        backoff_factor: float = 0.5,
        rate_throttle: float | None = None,
        variant: int = StoreVariant.DEFAULT,
        client: BotoClient | Callable[[], BotoClient] | None = None,
        host: str | None = None,
        key: str | None = None,
    ) -> None:
        """Initialize AppSync client.

        Args:
            env: Environment configuration.
            session_timeout: Session timeout in seconds.
            request_timeout: Request timeout in seconds.
            max_retries: Maximum number of retries.
            backoff_factor: Backoff factor for retries.
            rate_throttle: Rate throttle in requests per second.
            variant: Store variant to use.
            client: Boto3 client or client provider function.
            host: Custom host URL for bespoke variant.
            key: Custom API key for bespoke variant.
        """
        self._env = env
        self._configure_store_variant(variant=variant, host=host, key=key)

        self.variant = variant
        self._session_timeout = session_timeout
        self._request_timeout = request_timeout

        retry_strategy = Retry(
            total=max_retries,
            status_forcelist=[429, 500, 502, 503, 504],
            backoff_factor=backoff_factor,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self._http = requests.Session()
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)
        self._last_req_time: float = 0.0
        self._rate_throttle = rate_throttle

        if client is None:
            self._client_provider = _get_default_client
        else:
            self._client_provider = client if callable(client) else lambda: client
        self._client = None

    def _configure_store_variant(self, *, variant: int, host: str | None, key: str | None) -> None:
        """Configure store variant settings.

        Args:
            variant: Store variant type.
            host: Custom host for bespoke variant.
            key: Custom key for bespoke variant.

        Raises:
            ValueError: If variant configuration is invalid.
        """
        if variant == StoreVariant.WORD_DB:
            self.host = self._env.WordDB_SRS_url
            self.key = self._env.WordDB_SRS_key
        elif variant == StoreVariant.FEATURE_STORE:
            self.host = self._env.feature_store_url
            self.key = self._env.feature_store_key
        elif variant == StoreVariant.DEFAULT:
            self.host = self._env.SRS_url
            self.key = self._env.SRS_key
        elif variant == StoreVariant.ASSIGNMENT_SERVICE:
            self.host = self._env.assignment_service_SRS_url
            self.key = self._env.assignment_service_SRS_key
        elif variant == StoreVariant.SIS:
            self.host = self._env.SIS_uri
            self.key = self._env.SIS_key
        elif variant == StoreVariant.AOS:
            self.host = self._env.AOS_uri
            self.key = self._env.AOS_key
        elif variant == StoreVariant.BESPOKE:
            if host is None:
                raise ValueError("Missing bespoke store host")
            if key is None:
                logger.warning(f"Missing bespoke store key for host {host}")
                key = ""
            self.host = host
            self.key = key
        else:
            raise ValueError("Unknown store variant specified")

        if not self.host or ((not self.key) and (variant != StoreVariant.BESPOKE)):
            raise ValueError("Missing SRS host or key")

    @property
    def _feature_store_api_id(self) -> str:
        """Get feature store API ID lazily."""
        return cast(str, self._env.feature_store_api_id)

    @property
    def client(self) -> BotoClient:
        """Get or create boto3 client."""
        if self._client is None:
            self._client = self._client_provider()
        return self._client

    def __getstate__(self) -> dict[str, Any]:
        """Prepare object for pickle serialization."""
        result = self.__dict__
        result["_client"] = None
        return result

    def close(self) -> None:
        """Close HTTP session."""
        self._http.close()

    def query(self, *, query: str, query_name: str, variables: dict[str, Any]) -> Any:
        """Execute GraphQL query with retry logic.

        Args:
            query: GraphQL query string.
            query_name: Name of the query for logging.
            variables: Query variables.

        Returns:
            Query result data.

        Raises:
            AppsyncError: If query fails after retries.
        """
        if self._rate_throttle is not None:
            while time() - self._last_req_time < 1.0 / self._rate_throttle:
                delta = max(0, 1.0 / self._rate_throttle - time() + self._last_req_time)
                logger.debug(
                    f"Delaying {delta} seconds due to rate throttle of {self._rate_throttle} reqs/sec"
                )
                sleep(delta)
            self._last_req_time = time()

        def error_msg(*, error: dict[str, Any]) -> str | None:
            """Extract error message from GraphQL error."""
            msg = error.get("message")
            if msg is None:
                err_type = error.get("errorType")
                if err_type is not None:
                    msg = f"AWS generic error of type {err_type}"
                else:
                    msg = None
            return msg

        def retryable(*, errors: list[dict[str, Any]]) -> bool:
            """Check if errors are retryable."""

            def retryable_message(*, msg: str) -> bool:
                return any([m in msg for m in RETRYABLE_ERROR_MESSAGE_FRAGMENTS])

            return all([retryable_message(msg=error_msg(error=e) or "") for e in errors])

        def parse_errors(*, res: dict[str, Any]) -> tuple[str | None, bool]:
            """Parse errors from response."""
            try:
                msg = None
                retry = False
                errors = res.get("errors")
                if isinstance(errors, list):
                    msg = error_msg(error=errors[0])
                    if msg is None:
                        msg = f"Unparseable error from payload: {res}"
                    if retryable(errors=errors):
                        retry = True
            except Exception:
                msg = f"Query [{query_name}] - Unable to parse errors from payload: {res}"
                logger.exception(msg)
            return msg, retry

        headers = {
            "x-api-key": self.key,
        }

        data = {
            "query": query,
            "variables": variables,
        }

        backoff = 0.5
        retries = 4
        while retries >= 0:
            try:
                res = self._http.post(
                    self.host,
                    json=data,
                    headers=headers,
                    timeout=(self._session_timeout, self._request_timeout),
                ).json()

                err_msg, retry = parse_errors(res=res)
                if err_msg is not None:
                    if retry:
                        logger.warning(
                            f"Query [{query_name}] - SRS retry required (backoff={backoff}) for error {err_msg}"
                        )
                        sleep(backoff)
                        backoff *= 2.0
                        retries -= 1
                        continue
                    else:
                        raise Exception(f"Query [{query_name}] - {err_msg}")

                return res.get("data", {}).get(query_name)
            except Exception as e:
                raise AppsyncError(exception=e)
        raise Exception(f"Query [{query_name}] - Unable to complete after max retries")

    def get_edm_feature_schema(self) -> dict[str, Any]:
        """Get EDM feature schema from AppSync introspection.

        Returns:
            Dictionary mapping feature names to their schema definitions.

        Raises:
            AppsyncError: If ModelFeatures not found in schema.
        """
        res = self.client.get_introspection_schema(
            apiId=self._feature_store_api_id, format="JSON", includeDirectives=True
        )
        schema = json.loads(res["schema"].read().decode("utf-8"))

        for e in schema["data"]["__schema"]["types"]:
            if e["name"] == "ModelFeatures":
                result = {f["name"]: f for f in e["fields"]}
                return result
        raise AppsyncError(exception=ValueError("ModelFeatures not found in appsync schema"))

    def get_activity_features(self, *, activity_id: str) -> Any:
        """Extract EDM features for an activity.

        Args:
            activity_id: Activity identifier.

        Returns:
            Activity features data.
        """
        variables = {"activityId": activity_id}
        res = self.query(
            query=EXTRACT_FEATURES_QUERY,
            query_name="getModelFeaturesByActivityId",
            variables=variables,
        )
        return res

    @curry  # type: ignore
    def get_custom_activity_features(
        self, *, feature_list: list[str], activity_id: str, batch_limit: int = 5
    ) -> list[dict[str, Any]]:
        """Extract custom features for an activity.

        Args:
            feature_list: List of features to extract.
            activity_id: Activity identifier.
            batch_limit: Batch size limit for pagination.

        Returns:
            List of feature records.
        """
        feats = " ".join(feature_list)
        query = f"""
        query getCustomActivityFeats($activityId: String!, $nextToken: String) {{
          getModelFeaturesByActivityIdPaginated(activityId: $activityId, nextToken: $nextToken, limit:{int(batch_limit)}) {{
            nextToken
            items {{
                {feats}
            }}
          }}
        }}
        """

        next_token = ""
        result = []
        while True:
            variables = {"activityId": activity_id, "nextToken": next_token}
            res = self.query(
                query=query, query_name="getModelFeaturesByActivityIdPaginated", variables=variables
            )
            result.extend(res["items"])
            next_token = res["nextToken"]
            if (next_token is None) or (len(next_token) == 0):
                break

        return result

    def get_activity_details(
        self,
        *,
        activity_ids: list[str] | str,
        fields: list[str],
        filter: dict[str, Any] | None = None,
        batch_size: int = 500,
        show_progress: bool = False,
    ) -> list[dict[str, Any]]:
        """Extract activity details for given fields.

        Args:
            activity_ids: Activity ID(s) to query.
            fields: List of fields to retrieve.
            filter: Optional filter criteria.
            batch_size: Batch size for pagination.
            show_progress: Whether to show progress bar.

        Returns:
            List of activity detail records.
        """
        if isinstance(activity_ids, str):
            activity_ids = [activity_ids]

        fields_hier: dict[str, Any] = {}
        for f in fields:
            comps = f.split(".")
            ctx = fields_hier
            for c in comps:
                if c not in ctx:
                    ctx[c] = {}
                ctx = ctx[c]

        def field_lines(*, name: str | None, ctx: dict[str, Any], depth: int = 0) -> list[str]:
            """Generate field specification lines."""
            result = [f"{' ' * depth}{name}"] if name is not None else []
            if len(ctx) > 0:
                result.append(f"{' ' * depth}{{")
                for k, v in ctx.items():
                    result.extend(field_lines(name=k, ctx=v, depth=depth + 1))
                result.append(f"{' ' * depth}}}")
            return result

        field_spec = "\n".join(field_lines(name=None, ctx=fields_hier))

        if filter is None:
            filter_clause = ""
        else:
            filter_clause = f", filter: {{{_to_fields_clause(fields=filter)}}}"

        query = f"""
        query Q($activityIds: [String]!) {{
          getActivity(activityId: $activityIds {filter_clause})
            {field_spec}
        }}
        """

        results: list[dict[str, Any]] = []
        iterator: Iterable[int]
        progress: Any | None = None
        if show_progress:
            progress = tqdm(range(0, len(activity_ids), batch_size))
            iterator = progress
        else:
            iterator = range(0, len(activity_ids), batch_size)

        for i in iterator:
            batch = cast(
                list[dict[str, Any]],
                self.query(
                    query=query,
                    query_name="getActivity",
                    variables={"activityIds": activity_ids[i : i + batch_size]},
                ),
            )
            results.extend(batch)
            if progress is not None:
                progress.set_postfix({"processed": len(results), "total": len(activity_ids)})

        return results

    def get_activity_details_default(self, *, activity_id: str) -> list[dict[str, Any]]:
        """Extract default activity details.

        Args:
            activity_id: Activity identifier.

        Returns:
            Activity details with default fields.
        """
        return self.get_activity_details(
            activity_ids=activity_id, fields=GET_ACTIVITY_DEFAULT_FIELDS
        )

    def get_activity_phrase_flags(self, *, activity_id: str) -> list[dict[str, Any]]:
        """Extract whether flagging was triggered for each phrase.

        Args:
            activity_id: Activity identifier.

        Returns:
            Activity phrase flagging data.
        """
        return self.get_activity_details(
            activity_ids=activity_id, fields=GET_ACTIVITY_PHRASE_FLAGS_FIELDS
        )

    def get_expected_text(
        self, *, activity_ids: list[str], batch_size: int = 500, show_progress: bool = False
    ) -> list[dict[str, Any]]:
        """Get expected text from activities.

        Args:
            activity_ids: List of activity identifiers.
            batch_size: Batch size for pagination.
            show_progress: Whether to show progress bar.

        Returns:
            List of expected text records.
        """
        res: list[dict[str, Any]] = []
        iterator: Iterable[int]
        progress: Any | None = None
        if show_progress:
            progress = tqdm(range(0, len(activity_ids), batch_size))
            iterator = progress
        else:
            iterator = range(0, len(activity_ids), batch_size)

        for i in iterator:
            variables = {"activityId": activity_ids[i : i + batch_size]}

            this_res = cast(
                list[dict[str, Any]],
                self.query(
                    query=GET_EXPECTED_TEXT_QUERY,
                    query_name="getActivity",
                    variables=variables,
                ),
            )
            res += [
                {
                    AppSyncSchema.ACTIVITY_ID: r["activityId"],
                    AppSyncSchema.ACTIVITY_TYPE: r["type"],
                    AppSyncSchema.PHRASES: r["story"]["chapters"][0]["phrases"],
                    AppSyncSchema.STORY_GRADE: r["story"]["grade"],
                }
                for r in this_res
            ]
            if progress is not None:
                progress.set_postfix({"processed": len(res), "total": len(activity_ids)})

        return res

    def get_activities_student_ids(
        self, *, activity_ids: list[str], batch_size: int = 500, show_progress: bool = False
    ) -> list[str | None]:
        """Extract student IDs for a list of activities.

        Args:
            activity_ids: List of activity identifiers.
            batch_size: Batch size for pagination.
            show_progress: Whether to show progress bar.

        Returns:
            List of student IDs corresponding to activity IDs.
        """
        res: dict[str, str] = {}
        iterator: Iterable[int]
        progress: Any | None = None
        if show_progress:
            progress = tqdm(range(0, len(activity_ids), batch_size))
            iterator = progress
        else:
            iterator = range(0, len(activity_ids), batch_size)

        for i in iterator:
            variables = {"activityId": activity_ids[i : i + batch_size]}
            batch = cast(
                list[dict[str, Any]],
                self.query(
                    query=GET_ACTIVITIES_STUDENT_IDS_QUERY,
                    query_name="getActivity",
                    variables=variables,
                ),
            )
            found = {
                record["activityId"]: record["studentId"]
                for record in batch
                if record["studentId"] is not None
            }
            res.update(found)
            if progress is not None:
                progress.set_postfix({"processed": len(res), "total": len(activity_ids)})
        return [res.get(act_id) for act_id in activity_ids]

    def get_story_custom_fields(self, *, story_id: str, fields: list[str]) -> dict[str, Any]:
        """Get story record from storyId with custom fields.

        Args:
            story_id: Story identifier.
            fields: List of fields to retrieve.

        Returns:
            Story data with requested fields.
        """
        _id = str(uuid.UUID(story_id.translate(str.maketrans("", "", ",#"))))

        def parse_field_specifier(*, specifier: str) -> str:
            """Parse field specifier into GraphQL format."""
            result = ""
            for component in reversed(specifier.split(".")):
                if not result:
                    result = component
                else:
                    result = f"{component} {{\n{result}\n}}"
            return result

        query = f'''
            query GetStory{{
              getAmiraStoryById(storyid: "{_id}") {{
                {SLASH_N.join([parse_field_specifier(specifier=f) for f in fields])}
              }}
            }}
            '''

        return cast(
            dict[str, Any], self.query(query=query, query_name="getAmiraStoryById", variables={})
        )

    def get_story_info(self, *, story_id: str, extra_fields: list[str] | None = None) -> list[Any]:
        """Retrieve basic story information.

        Args:
            story_id: Story identifier.
            extra_fields: Optional list of extra fields to include.

        Returns:
            List containing phrases, item types, grade level, and extra fields.
        """
        story_cache_fields = copy(GET_STORY_CACHE_DEFAULT_FIELDS)
        if extra_fields is not None:
            story_cache_fields += extra_fields
        res = self.get_story_custom_fields(story_id=story_id, fields=story_cache_fields)

        def clean_items(*, items: list[dict[str, Any]] | None, phrases: list[str]) -> list[str]:
            """Clean and validate item types."""
            if items is None or len(items) != len(phrases):
                return ["phrase"] * len(phrases)
            return [entry["type"] for entry in items]

        phrases = res["chapters"][0]["phrases"]
        items = res["chapters"][0]["items"]
        try:
            grade = int(res["grade"])
        except ValueError:
            grade = 0
            logger.warning(f"Story {story_id} has unparsable grade {res['grade']}")
        output_list = [phrases, clean_items(items=items, phrases=phrases), grade]

        def parse_res(*, r: dict[str, Any], f: str) -> Any:
            """Parse response for specific field."""
            f_comp = f.split(".")

            def recurse(*, d: dict[str, Any], comp: list[str]) -> Any:
                if len(comp) == 1:
                    return d[comp[0]]
                else:
                    return [recurse(d=e, comp=comp[1:]) for e in d[comp[0]]]

            return recurse(d=r, comp=f_comp)

        if extra_fields is not None:
            output_list += [parse_res(r=res, f=x) for x in extra_fields]

        return output_list

    def get_story_grade(self, *, story_id: str) -> str:
        """Get story grade level.

        Args:
            story_id: Story identifier.

        Returns:
            Story grade level.
        """
        res = self.get_story_custom_fields(story_id=story_id, fields=["grade"])
        return cast(str, res["grade"])

    def get_word_data(
        self, *, word: str, query_list: list[str], locale: str = "en_US"
    ) -> list[dict[str, Any]]:
        """Get word database information for a word.

        Args:
            word: Word to query.
            query_list: List of fields to retrieve.
            locale: Locale for the query.

        Returns:
            List containing word data.

        Raises:
            ValueError: If not using WORD_DB variant.
        """
        if self.variant != StoreVariant.WORD_DB:
            raise ValueError("Query only for WORD_DB variant")

        query = f'''
        query Get {{
          getPsycholinguisticsStore(WORD:"{word.upper().replace('"', "")}" filter: {{ locale: {locale} }}) {{
            {" ".join(query_list)}
          }}
        }}
        '''
        res = cast(
            dict[str, Any] | None,
            self.query(query=query, query_name="getPsycholinguisticsStore", variables={}),
        )

        if res is not None:
            temp = {k: _norm_field(name=k, value=v) for k, v in res.items()}
            return [temp]
        else:
            return []

    def get_all_words(
        self, *, locale: str = "en_US", limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Extract all words with pagination.

        Args:
            locale: Locale for word retrieval.
            limit: Maximum number of words to retrieve.

        Returns:
            List of word records.
        """
        query = """
        query q($nextToken: String) {
            listWordsWithPagination(inputData: {nextToken: $nextToken, filter: {locale: es_MX}, limit: 50}) {
                nextToken
                items {
                  WORD
                  AREA_AOA
                }
            }
        }
        """

        next_token = ""
        result: list[dict[str, Any]] = []
        while len(result) < (limit if limit is not None else 1e6):
            variables = {"nextToken": next_token}
            res = cast(
                dict[str, Any],
                self.query(query=query, query_name="listWordsWithPagination", variables=variables),
            )
            items = cast(list[dict[str, Any]], res["items"])
            result.extend(items)
            next_token = res["nextToken"]
            if (next_token is None) or (len(next_token) == 0):
                break
            print(f"{len(result)} words gathered")
            sleep(1.0)

        return result

    def get_word_phon_grapheme_info(self, *, word: str) -> dict[str, str]:
        """Get phonetic and graphemic information for a word.

        Args:
            word: Word to query.

        Returns:
            Dictionary with PHON and GRAPHEMIC_BREAKDOWN keys.
        """
        query = f'''
        query Q {{
          word(WORD: "{word}") {{
            items {{
              PHON
              GRAPHEMIC_BREAKDOWN
            }}
          }}
        }}
        '''
        res = cast(dict[str, Any], self.query(query=query, query_name="word", variables={}))
        items = cast(list[dict[str, str]], res.get("items", []))
        if len(items) == 1:
            return items[0]
        return {"PHON": "", "GRAPHEMIC_BREAKDOWN": ""}

    def get_word_is_metadata(self, *, word: str, locale: str = "en_US") -> dict[str, Any] | None:
        """Get all metadata relevant for firing interventions.

        Args:
            word: Word to query.
            locale: Locale for the query.

        Returns:
            Dictionary of word metadata or None if not found.

        Raises:
            ValueError: If not using WORD_DB variant.
        """
        if self.variant != StoreVariant.WORD_DB:
            raise ValueError("Query only for WORD_DB variant")

        query = f'''
        query Get {{
          getPsycholinguisticsStore(WORD:"{word.upper()}" filter: {{ locale: {locale} }}) {{
            PHON
            GRAPHEMIC_BREAKDOWN
            NSYL
            COGNATE {{
                spanish
            }}
            DEFINITION {{
                default
            }}
            FUNFACTS_DESCRIPTION
            FUNFACTS_IMG_URL
            PHONEME_VIDEO_URL
            IMG_URL
            VIDEO_URL
            MORPHEME
            MORPHEME_POSITION
            MORPHEME_MEANING
            MORPHEME_ALTS
            NAME_NATIONALITY
            NAME_MEANING
            NAME_PERSON
            NAME_FLAG
            RHYME
            RIDDLE
            RIDDLE_ANSWER
            SIGHT
          }}
        }}
        '''
        res = cast(
            dict[str, Any] | None,
            self.query(query=query, query_name="getPsycholinguisticsStore", variables={}),
        )

        if res is not None:
            temp = {k: _norm_field(name=k, value=v) for k, v in res.items()}
            return temp
        else:
            return None

    def get_yellow_flag_fraction(
        self, *, activity_ids: list[str], batch_size: int = 500, show_progress: bool = False
    ) -> list[dict[str, Any]]:
        """Get fraction of phrases that were yellow-flagged.

        Args:
            activity_ids: List of activity identifiers.
            batch_size: Batch size for pagination.
            show_progress: Whether to show progress bar.

        Returns:
            List of yellow flag fraction records.
        """
        logger.info(
            f"getting yellow flag phrase fractions from SRS for {len(activity_ids)} activities"
        )

        res: list[dict[str, Any]] = []
        iterator: Iterable[int]
        progress: Any | None = None
        if show_progress:
            progress = tqdm(range(0, len(activity_ids), batch_size))
            iterator = progress
        else:
            iterator = range(0, len(activity_ids), batch_size)

        for i in iterator:
            variables = {"activityId": activity_ids[i : i + batch_size]}

            this_res = cast(
                list[dict[str, Any]],
                self.query(
                    query=GET_YELLOW_FLAG_FRACTION_QUERY,
                    query_name="getActivity",
                    variables=variables,
                ),
            )
            res.extend(
                [
                    {
                        AppSyncSchema.ACTIVITY_ID: r["activityId"],
                        AppSyncSchema.YELLOW_PHRASE_FRACTION: sum(
                            [1 if phr["triggered"] else 0 for phr in r["flagging"]]
                        )
                        / len(r["flagging"])
                        if len(r["flagging"]) > 0
                        else 1.1,
                        AppSyncSchema.YELLOW_PHRASE: [
                            1 if phr["triggered"] else 0 for phr in r["flagging"]
                        ],
                    }
                    for r in this_res
                ]
            )
            if progress is not None:
                progress.set_postfix({"processed": len(res), "total": len(activity_ids)})
        return res

    def get_activities(
        self,
        *,
        activity_type: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        limit: int = 10,
        inter_batch_sleep: float | None = None,
    ) -> Any:
        """Extract activities with filtering and pagination.

        Args:
            activity_type: Optional activity type filter.
            status: Optional status filter.
            start_date: Optional start date filter.
            end_date: Optional end date filter.
            limit: Maximum number of activities to retrieve.
            inter_batch_sleep: Sleep time between batches.

        Returns:
            DataFrame of activity data.
        """

        def make_query(*, end_date_override: datetime | None, batch_size: int) -> str:
            """Generate query with filters."""
            filters = []
            if activity_type is not None:
                filters.append(f'activityType: "{activity_type}"')
            if status is not None:
                filters.append(f'status: "{status}"')
            if start_date is not None:
                filters.append(f'dateStart: "{start_date.strftime(TIME_FORMATTER)}"')
            if end_date_override is not None:
                filters.append(f'dateEnd: "{end_date_override.strftime(TIME_FORMATTER)}"')

            if len(filters) > 0:
                filter_str = f"filter: {{{','.join(filters)}}},"
            else:
                filter_str = ""

            query = f"""
            query MyQuery {{
              activities({filter_str} limit: {batch_size}) {{
                story {{
                  chapters {{
                    phrases
                  }}
                }}
                activityId,
                createdAt,
                errors
              }}
            }}
            """

            return query

        batch_end_time = end_date
        result: list[list[Any]] = []
        activities: set[str] = set()
        while limit > 0:
            logger.info(f"Activities so far {len(activities)}")
            batch_size = min(MAX_ACTIVITIES_BATCH_SIZE, limit + 1)
            query = make_query(end_date_override=batch_end_time, batch_size=batch_size)
            res = cast(
                list[dict[str, Any]],
                self.query(query=query, query_name="activities", variables={}),
            )
            last_create_time: datetime | None = None
            unique_count = 0
            for item in res:
                activity_id = item["activityId"]
                if activity_id not in activities:
                    if limit - unique_count == 0:
                        break
                    unique_count += 1
                    activities.add(activity_id)
                    last_create_time = datetime.strptime(item["createdAt"], TIME_FORMATTER)
                    for phrase_idx, phrase in enumerate(item["story"]["chapters"][0]["phrases"]):
                        if phrase_idx >= len(item["errors"]):
                            break
                        for word_idx, word in enumerate(phrase.split()):
                            result.append(
                                [
                                    activity_id,
                                    phrase_idx,
                                    word_idx,
                                    word,
                                    item["errors"][phrase_idx][word_idx],
                                ]
                            )
            if unique_count == 0:
                break
            limit -= unique_count
            batch_end_time = last_create_time
            if inter_batch_sleep is not None:
                sleep(inter_batch_sleep)

        return pd.DataFrame(
            result, columns=["activity_id", "phrase_index", "word_index", "expected_text", "errors"]
        )

    def get_activities_status(self, *, activity_ids: list[str]) -> list[dict[str, Any]]:
        """Get activity status information.

        Args:
            activity_ids: List of activity identifiers.

        Returns:
            List of activity status records.
        """
        variables = {"activityId": activity_ids}
        res = cast(
            list[dict[str, Any]],
            self.query(
                query=GET_ACTIVITIES_STATUS_QUERY, query_name="getActivity", variables=variables
            ),
        )
        return res

    def set_activity_fields(
        self, *, activity_id: str, field_values: dict[str, Any]
    ) -> dict[str, Any]:
        """Set activity field values.

        Args:
            activity_id: Activity identifier.
            field_values: Dictionary of field names and values to set.

        Returns:
            Updated activity data.
        """
        fields_clause = _to_fields_clause(fields=field_values)
        query = f'''
            mutation setFields {{
                updateActivity(activityId: "{activity_id}", {fields_clause}) {{
                    activityId
                    {" ".join(field_values.keys())}
                }}
            }}
        '''
        data = cast(
            dict[str, Any], self.query(query=query, query_name="updateActivity", variables={})
        )
        return data

    def set_stage_spanish_activity(self, *, activity_id: str) -> dict[str, Any]:
        """Set activity as Spanish stage activity.

        Args:
            activity_id: Activity identifier.

        Returns:
            Updated activity data.
        """
        return self.set_activity_fields(activity_id=activity_id, field_values={"tags": "ES_MX"})

    def get_assignment_activities_v2(
        self, *, assignment_id: str, fields: Iterable[str] = DEFAULT_ASSIGNMENT_ACTIVITY_FIELDS
    ) -> list[dict[str, Any]]:
        """Get assignment activities with custom fields.

        Args:
            assignment_id: Assignment identifier.
            fields: Fields to retrieve for each activity.

        Returns:
            List of activity records.
        """
        query = GET_ASSIGNMENT_ACTIVITIES_QUERY.format(fields=" ".join(fields))
        data = cast(
            list[dict[str, Any]],
            self.query(
                query=query,
                query_name="assignmentActivities",
                variables={"assignmentId": assignment_id},
            ),
        )
        activities = cast(list[dict[str, Any]], data[0]["activities"])
        return activities

    def get_assignment_activities(self, *, assignment_id: str) -> list[str]:
        """Get assignment activity IDs.

        Args:
            assignment_id: Assignment identifier.

        Returns:
            List of activity IDs.
        """
        return [
            a["activityId"] for a in self.get_assignment_activities_v2(assignment_id=assignment_id)
        ]

    def create_assignment(self, *, student_id: str, tasks: list[ActivitySpecifier]) -> str:
        """Create a new assignment.

        Args:
            student_id: Student identifier.
            tasks: List of activity specifiers for the assignment.

        Returns:
            Created assignment ID.
        """

        def manifest_query_term(*, task: ActivitySpecifier, is_last: bool) -> str:
            """Generate manifest query term for a task."""
            return f'{{activityType: "{task.activity_type}" locale: {task.locale} sequence: {"BREAK" if is_last else "CONTINUE"}}}'

        manifest_clause = ",".join(
            [
                manifest_query_term(task=s, is_last=idx == len(tasks) - 1)
                for idx, s in enumerate(tasks)
            ]
        )
        query = f'''
            mutation AddAssignment {{
                addAssignment(input: {{
                    entityId: "{student_id}"
                    entityType: STUDENT
                    assignor: "TEST"
                    assignmentType: "BENCHMARK"
                    manifest: [{manifest_clause}]
                    }}) {{
                    assignmentId
                }}
            }}
        '''
        data = cast(
            dict[str, Any], self.query(query=query, query_name="addAssignment", variables={})
        )
        return cast(str, data["assignmentId"])

    @staticmethod
    def _cast_nan(*, val: Any, default: Any = None) -> Any:
        """Cast NaN values to default.

        Args:
            val: Value to check and cast.
            default: Default value to use for NaN.

        Returns:
            Original value or default if NaN.
        """
        if val is not None and not isinstance(val, str) and math.isnan(val):
            return default
        return val

    @staticmethod
    def _format_query(
        *, features: dict[str, Any], student_id: str, phrase_index: int
    ) -> tuple[str, dict[str, Any]]:
        """Format features for GraphQL mutation.

        Args:
            features: Feature dictionary to format.
            student_id: Student identifier.
            phrase_index: Phrase index.

        Returns:
            Tuple of (query_string, variables).
        """
        float_pattern = re.compile(r"^-?\d*\.?\d+(?:[Ee]-?\d+)?$")

        def leaf_is_type(*, t: type, v: Any) -> bool:
            """Check if leaf value is of specified type."""
            if (isinstance(v, list) or isinstance(v, np.ndarray)) and len(v) > 0:
                return leaf_is_type(t=t, v=v[0])
            else:
                return isinstance(v, t)

        str_cols = [k for k in features if leaf_is_type(t=str, v=features[k])]

        input_data = {}
        for k, v in features.items():
            if isinstance(v, list):
                if k in str_cols:
                    v = [str(_v) for _v in v]

                if len(v) > 0 and isinstance(v[0], np.ndarray):

                    def replace_nan(value: Any) -> Any:
                        if isinstance(value, list):
                            return [replace_nan(item) for item in value]
                        if isinstance(value, float) and math.isnan(value):
                            return None
                        return value

                    converted: list[Any] = []
                    for _v in v:
                        arr_list = cast(np.ndarray, _v).tolist()
                        converted.append(replace_nan(arr_list))
                    val = converted
                else:
                    val = []
                    for _v in v:
                        normalized = AppSync._cast_nan(val=_v)
                        if normalized is None:
                            val.append(None)
                        elif float_pattern.match(str(normalized)):
                            val.append(float(normalized))
                        else:
                            val.append(normalized)
            elif isinstance(v, np.ndarray):
                val = v.tolist()
                for i, sub in enumerate(val):
                    if isinstance(sub, np.ndarray):
                        val[i] = sub.tolist()
            else:
                if k in str_cols:
                    v = str(v)
                val = v
            input_data[k] = val

        created_at = int(str(random.randint(1, 9999999))[-7:] + str(phrase_index).zfill(2))

        return SET_MODEL_FEATURES_MUTATION, {
            "studentId": student_id,
            "createdAt": created_at,
            "input": input_data,
        }

    def set_model_features(
        self,
        *,
        activity_id: str,
        student_id: str,
        model: str,
        phrase_index: int,
        features: dict[str, Any],
    ) -> None:
        """Set EDM features for an activity.

        Args:
            activity_id: Activity identifier.
            student_id: Student identifier.
            model: Model that consumed the features.
            phrase_index: Phrase index the features are for.
            features: Features to set.
        """
        all_features = {
            **features,
            "phraseIndex": phrase_index,
            "activityId": activity_id,
            "model": model,
        }
        query, variables = AppSync._format_query(
            features=all_features, student_id=student_id, phrase_index=phrase_index
        )
        self.query(query=query, query_name="setModelFeatures", variables=variables)

    def get_student_grade(self, *, student_id: str) -> str | None:
        """Get student grade level from SIS.

        Args:
            student_id: Student identifier.

        Returns:
            Student grade level or None if not found.

        Raises:
            ValueError: If not using SIS variant or unexpected grade value.
        """
        if self.variant != StoreVariant.SIS:
            raise ValueError("Query only for SIS variant")

        query = GET_STUDENT_GRADE_QUERY.format(studentId=uuid.UUID(str(student_id)))
        data = self.query(query=query, query_name="student", variables={})
        grade = data.get("grade")
        if grade is not None:
            if grade.upper() in GRADE_STR_TO_CHAR:
                return GRADE_STR_TO_CHAR[grade.upper()]
            else:
                raise ValueError(f"Unexpected grade value found for student {student_id}: {grade}")
        return None

    def get_activities_noise_detector(
        self, *, activity_ids: list[str], batch_size: int = 500, show_progress: bool = False
    ) -> dict[str, dict[str, list[bool]]]:
        """Get noise detector values for a list of activities.

        Args:
            activity_ids: List of activity identifiers.
            batch_size: Batch size for pagination.
            show_progress: Whether to show progress bar.

        Returns:
            Dictionary mapping activity IDs to noise detector results.
        """
        res: dict[str, dict[str, list[Any]]] = {}
        iterator: Iterable[int]
        progress: Any | None = None
        if show_progress:
            progress = tqdm(range(0, len(activity_ids), batch_size))
            iterator = progress
        else:
            iterator = range(0, len(activity_ids), batch_size)

        for i in iterator:
            variables = {"activityId": activity_ids[i : i + batch_size]}
            batch = cast(
                list[dict[str, Any]],
                self.query(
                    query=GET_ACTIVITIES_NOISE_DETECTOR_QUERY,
                    query_name="getActivity",
                    variables=variables,
                ),
            )
            for record in batch:
                new_choppy_per_phrase = []
                new_quiet_per_phrase = []
                new_SNR_level = []

                for x in record["flagging"]:
                    for n in x.get("detectors"):
                        if n.get("name") == "choppy_audio_detector_v2":
                            new_choppy_per_phrase.append(n.get("triggered"))
                        if n.get("name") == "MagicLowAudio":
                            new_quiet_per_phrase.append(n.get("triggered"))
                        if n.get("name") == "background_noise_detector_v2":
                            new_SNR_level.append(n.get("triggered"))

                    found = {
                        record["activityId"]: {
                            "triggered_choppy": new_choppy_per_phrase,
                            "triggered_quiet": new_quiet_per_phrase,
                            "triggered_noisy": new_SNR_level,
                        }
                    }
                    res.update(found)
            if progress is not None:
                progress.set_postfix({"processed": len(res), "total": len(activity_ids)})
        return res

    def set_aos_scores(
        self,
        *,
        assignment_id: str,
        student_id: str,
        school_id: str,
        district_id: str,
        school_year: int,
        period: int,
        created_at: str,
        grade: int,
        target_activities: list[str] | dict[str, str],
        amira_scores: dict[str, float],
        ISIP_scores: dict[str, float],
        calculated_properties: dict[str, float],
    ) -> dict[str, Any]:
        """Set AOS scaling scores.

        Args:
            assignment_id: Assignment identifier.
            student_id: Student identifier.
            school_id: School identifier.
            district_id: District identifier.
            school_year: School year.
            period: Period number.
            created_at: Creation timestamp.
            grade: Student grade.
            target_activities: List of activity IDs or dict of activity ID to status.
            amira_scores: Amira score dictionary.
            ISIP_scores: ISIP score dictionary.
            calculated_properties: Calculated properties dictionary.

        Returns:
            AOS response data.

        Raises:
            ValueError: If not using AOS variant.
        """
        if self.variant != StoreVariant.AOS:
            raise ValueError("Query only for AOS variant")

        def dict_clause(*, scores: dict[str, Any]) -> str:
            """Generate dictionary clause for GraphQL."""

            def encode(*, v: Any) -> str:
                if isinstance(v, str):
                    return f'"{v}"'
                else:
                    return str(v)

            return ", ".join(
                [f"{k}: {encode(v=v) if v is not None else 'null'}" for k, v in scores.items()]
            )

        def make_subscores_clause(*, name: str, scores: dict[str, Any]) -> str:
            """Generate subscores clause."""
            return f"{name}: {{{dict_clause(scores=scores)}}}"

        def make_target_activities_clause(*, activities: list[str] | dict[str, str]) -> str:
            """Generate target activities clause."""
            if isinstance(activities, list):
                return f"targetActivities: {json.dumps(activities)}"
            else:
                activity_statuses = [
                    "{" + dict_clause(scores={"activityId": k, "status": v}) + "}"
                    for k, v in activities.items()
                ]
                return f"targetActivityStatuses: {'[' + ', '.join(activity_statuses) + ']'}"

        query = f'''
            mutation setScores {{
                setScores(assignmentId: "{assignment_id}",
                          {make_target_activities_clause(activities=target_activities)},
                          studentId: "{student_id}",
                          createdAt: "{created_at}",
                          studentGrade: {grade},
                          schoolId: "{school_id}",
                          districtId: "{district_id}",
                          schoolYear: {school_year},
                          period: {period},
                          {make_subscores_clause(name="amiraScores", scores=amira_scores)},
                          {make_subscores_clause(name="ISIPScores", scores=ISIP_scores)},
                          {make_subscores_clause(name="calculatedProperties", scores=calculated_properties)}) {{
                    status
                }}
            }}
        '''
        data = cast(dict[str, Any], self.query(query=query, query_name="setScores", variables={}))
        return data

    def get_aos_priors(self) -> dict[int, dict[str, Any]]:
        """Get grade theta priors from AOS.

        Returns:
            Dictionary mapping grades to their theta priors.

        Raises:
            ValueError: If not using AOS variant.
        """
        if self.variant != StoreVariant.AOS:
            raise ValueError("Query only for AOS variant")

        data = cast(
            list[dict[str, Any]],
            self.query(query=GET_AOS_PRIORS_QUERY, query_name="getDefaultThetas", variables={}),
        )
        return {e["grade"]: e for e in data}
