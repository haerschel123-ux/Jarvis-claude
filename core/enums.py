"""Every mode, level and state JARVIS knows, in one place.

These names are part of the API contract with the web UI, so they are serialised as their
string values and must stay stable.
"""

from __future__ import annotations

from enum import Enum, StrEnum

# StrEnum members serialise to their value in JSON and compare equal to plain strings,
# which is what the web UI and the tool schemas rely on.


# --- Autonomy and permissions (Spec §15, §16, §68) --------------------------------------


class AutonomyLevel(int, Enum):
    """Global autonomy. Higher levels let JARVIS act without asking first."""

    READ_ONLY = 0
    ASK_EVERYTHING = 1
    ASK_RISKY = 2
    AUTO_TRUSTED = 3
    FULL_AUTO = 4


class PermissionValue(StrEnum):
    DENY = "DENY"
    ASK = "ASK"
    ALLOW = "ALLOW"


class Capability(StrEnum):
    """Per-capability permission slots (Spec §16)."""

    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    TERMINAL_READ = "terminal_read"
    TERMINAL_WRITE = "terminal_write"
    TERMINAL_ADMIN = "terminal_admin"
    MOUSE = "mouse"
    KEYBOARD = "keyboard"
    SCREEN_CAPTURE = "screen_capture"
    PROGRAM_LAUNCH = "program_launch"
    PROGRAM_CLOSE = "program_close"
    GIT_COMMIT = "git_commit"
    GIT_PUSH = "git_push"
    EMAIL_READ = "email_read"
    EMAIL_SEND = "email_send"
    CALENDAR_READ = "calendar_read"
    CALENDAR_WRITE = "calendar_write"
    DISCORD = "discord"
    NITRADO = "nitrado"
    SMART_HOME = "smart_home"
    WEB_SEARCH = "web_search"
    DOWNLOADS = "downloads"


class RiskLevel(StrEnum):
    """Intrinsic risk of a tool, independent of who calls it (Spec §68)."""

    SAFE_READ = "SAFE_READ"
    SAFE_ACTION = "SAFE_ACTION"
    WRITE = "WRITE"
    EXTERNAL_WRITE = "EXTERNAL_WRITE"
    SYSTEM_CONTROL = "SYSTEM_CONTROL"
    DESTRUCTIVE = "DESTRUCTIVE"
    PRIVILEGED = "PRIVILEGED"


RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.SAFE_READ: 0,
    RiskLevel.SAFE_ACTION: 1,
    RiskLevel.WRITE: 2,
    RiskLevel.EXTERNAL_WRITE: 3,
    RiskLevel.SYSTEM_CONTROL: 4,
    RiskLevel.DESTRUCTIVE: 5,
    RiskLevel.PRIVILEGED: 6,
}


class PermissionDecision(StrEnum):
    ALLOWED = "allowed"
    NEEDS_CONFIRMATION = "needs_confirmation"
    DENIED = "denied"


# --- Command classification (Spec §47) --------------------------------------------------


class CommandClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    WRITE = "WRITE"
    SYSTEM = "SYSTEM"
    DESTRUCTIVE = "DESTRUCTIVE"
    ADMIN = "ADMIN"


# --- Model routing (Spec §11) -----------------------------------------------------------


class RouterMode(StrEnum):
    AUTO = "AUTO"
    MANUAL = "MANUAL"
    FREE_ONLY = "FREE_ONLY"
    LOCAL_ONLY = "LOCAL_ONLY"
    HYBRID = "HYBRID"
    MULTI_MODEL = "MULTI_MODEL"


class ModelCapability(StrEnum):
    """Capability axes a model is scored on (Spec §12)."""

    CHAT = "chat"
    CODING = "coding"
    VISION = "vision"
    TOOLS = "tools"
    REASONING = "reasoning"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_CONTEXT = "long_context"
    SPEED = "speed"
    FREE = "free"
    LOCAL = "local"


# --- Assistant core (Spec §13) ----------------------------------------------------------


class Intent(StrEnum):
    CONVERSATION = "CONVERSATION"
    QUESTION = "QUESTION"
    WEB_RESEARCH = "WEB_RESEARCH"
    FILE_TASK = "FILE_TASK"
    CODING_TASK = "CODING_TASK"
    SYSTEM_ACTION = "SYSTEM_ACTION"
    SCREEN_ACTION = "SCREEN_ACTION"
    MEMORY_ACTION = "MEMORY_ACTION"
    REMINDER = "REMINDER"
    CALENDAR_ACTION = "CALENDAR_ACTION"
    EMAIL_ACTION = "EMAIL_ACTION"
    DISCORD_ACTION = "DISCORD_ACTION"
    GITHUB_ACTION = "GITHUB_ACTION"
    DAYZ_ACTION = "DAYZ_ACTION"
    SMART_HOME_ACTION = "SMART_HOME_ACTION"
    MULTI_STEP_TASK = "MULTI_STEP_TASK"


class AssistantState(StrEnum):
    """Voice orb / status states (Spec §56)."""

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    RECOGNIZING = "RECOGNIZING"
    THINKING = "THINKING"
    ACTING = "ACTING"
    SPEAKING = "SPEAKING"
    ERROR = "ERROR"
    PAUSED = "PAUSED"


# --- Voice (Spec §4, §5, §6) ------------------------------------------------------------


class VoiceMode(StrEnum):
    OFF = "OFF"
    PUSH_TO_TALK = "PUSH_TO_TALK"
    WAKE_WORD = "WAKE_WORD"
    CONTINUOUS_CONVERSATION = "CONTINUOUS_CONVERSATION"


class WakeEngine(StrEnum):
    OPENWAKEWORD = "openwakeword"
    STT_MATCH = "stt_match"


# --- Screen (Spec §18) ------------------------------------------------------------------


class ScreenMode(StrEnum):
    SCREEN_OFF = "SCREEN_OFF"
    SCREENSHOT_ON_REQUEST = "SCREENSHOT_ON_REQUEST"
    ACTIVE_WINDOW_WATCH = "ACTIVE_WINDOW_WATCH"
    LIVE_ASSIST = "LIVE_ASSIST"


# --- Memory (Spec §26) ------------------------------------------------------------------


class MemoryMode(StrEnum):
    OFF = "OFF"
    MANUAL = "MANUAL"
    IMPORTANT_ONLY = "IMPORTANT_ONLY"
    AUTO = "AUTO"


class MemoryKind(StrEnum):
    """Memory categories (Spec §24)."""

    PREFERENCE = "preference"
    PROJECT = "project"
    ENTITY = "entity"
    TASK = "task"
    FACT = "fact"
    WORKFLOW = "workflow"


# --- Proactivity (Spec §14) -------------------------------------------------------------


class ProactivityMode(StrEnum):
    OFF = "OFF"
    IMPORTANT_ONLY = "IMPORTANT_ONLY"
    NORMAL = "NORMAL"
    PROACTIVE = "PROACTIVE"


# --- Coding agent (Spec §34) ------------------------------------------------------------


class CodingMode(StrEnum):
    CHAT = "CHAT"
    PLAN_ONLY = "PLAN_ONLY"
    ASK_BEFORE_EDIT = "ASK_BEFORE_EDIT"
    AUTO_EDIT = "AUTO_EDIT"
    FULL_AGENT = "FULL_AGENT"


# --- Tasks (Spec §102) ------------------------------------------------------------------


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_PERMISSION = "WAITING_PERMISSION"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}


# --- Integrations (Spec §61) ------------------------------------------------------------


class IntegrationStatus(StrEnum):
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    ERROR = "Error"


# --- Scheduler (Spec §27) ---------------------------------------------------------------


class ScheduleKind(StrEnum):
    ONE_TIME = "one_time"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    INTERVAL = "interval"
