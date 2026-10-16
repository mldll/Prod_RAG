import os
from dotenv import load_dotenv
load_dotenv()

# Text chunking parameters
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50

# Retrieval parameters
K_RETRIEVAL = 4
K_EVALUATION = 10

# Agent parameters
MAX_RETRIES = 2

# Model used
EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_LOCAL_MODEL = "all-MiniLM-L6-v2"
# LLM providers: groq, gemini, or claude_local (the claude CLI on this machine with your own Claude subscription;
# local runs only, see src/core/claude_cli.py). EVAL_LLM_MODEL is the judge and ingest-batch's question writer.
MAIN_LLM_MODEL = os.getenv("MAIN_LLM_MODEL", "groq")
EVAL_LLM_MODEL = os.getenv("EVAL_LLM_MODEL", "groq")
# claude_local: the model alias (haiku, sonnet or opus), the thinking effort, CLI processes at once, seconds per call
CLAUDE_LOCAL_MODEL = os.getenv("CLAUDE_LOCAL_MODEL", "haiku")
CLAUDE_LOCAL_EFFORT = os.getenv("CLAUDE_LOCAL_EFFORT", "low")
CLAUDE_LOCAL_MAX_CONCURRENCY = int(os.getenv("CLAUDE_LOCAL_MAX_CONCURRENCY", "2"))
CLAUDE_LOCAL_TIMEOUT_SECONDS = 120
# The Groq SDK retries a rate limit (429) or server error this many times, waiting for Groq's retry-after header
# between attempts when it's 60 s or less (0.5-8 s otherwise). Every wait counts against the caller's timeout
# (live-eval's requests: 60 s).
GROQ_MAX_RETRIES = 2

# Database & Runtime Storage paths
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Seed state baked into the image at build time (read-only on AWS Lambda)
SEED_DATA_DIR = os.path.join(BASE_DIR, "data")
SEED_LOCAL_DIR = os.path.join(BASE_DIR, ".local")

# Working copy the app reads and writes. Defaults to the seed dirs locally
LOCAL_DIR = os.path.abspath(os.getenv("LOCAL_DIR", SEED_LOCAL_DIR))
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", SEED_DATA_DIR))
INGESTED_DATA_DIR = os.path.join(DATA_DIR, "ingested")
CHROMA_PERSIST_DIR = os.path.join(LOCAL_DIR, "chroma_db")

# Logging configuration
LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_TO_CONSOLE = os.getenv("LOG_TO_CONSOLE", "true").lower() in ("1", "true", "yes")
LOG_TO_FILE = os.getenv("LOG_TO_FILE", "true").lower() in ("1", "true", "yes")
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", os.path.join("logs", "app.log"))
LOG_FORMAT = '%(message)s'
LOG_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'
CAPTURE_EXTERNAL_LOGS = False  # Capture logs from external libraries

ROUTING_METHOD = os.getenv("ROUTING_METHOD", "classical")  # "llm", "classical"
# t-SNE picture of all chunk embeddings on every retrain: minutes of CPU on a CI runner, so the workflow turns it off
GENERATE_VISUALIZATION = os.getenv("GENERATE_VISUALIZATION", "true").lower() in ("1", "true", "yes")
CLUSTERS_DIR = os.path.join(LOCAL_DIR, "clusters")
CLASSIFIER_MODEL_PATH = os.path.join(CLUSTERS_DIR, "retrieval_classifier.joblib")
# Corpus probe: a chunk at least this similar (cosine) to the query overrules the classifier's "no retrieval".
CORPUS_PROBE_THRESHOLD = float(os.getenv("CORPUS_PROBE_THRESHOLD", "0.55"))
# Probe floor: an unsure "retrieve" vote (confidence below the second value) only stands if some chunk is at least
# this similar. Otherwise no document covers the question, and searching would only end in "I don't know".
CORPUS_PROBE_FLOOR = float(os.getenv("CORPUS_PROBE_FLOOR", "0.45"))
CORPUS_PROBE_FLOOR_MAX_CONFIDENCE = float(os.getenv("CORPUS_PROBE_FLOOR_MAX_CONFIDENCE", "0.65"))
# Generated questions: ingest-batch has the LLM write this many Q/A pairs per new or changed document
QUESTIONS_PER_DOC = 5
QUESTION_GEN_PAUSE_SECONDS = float(os.getenv("QUESTION_GEN_PAUSE_SECONDS", "0"))  # raise if the LLM's rate limit bites

ENABLE_SEMANTIC_CACHE = True
CACHE_SIMILARITY_THRESHOLD = 0.95
SEMANTIC_CACHE_DIR = os.path.join(LOCAL_DIR, "s_cache")

# Preferred models per provider (tried in order, first available wins)
PREFERRED_MODELS = {
    "groq": ["openai/gpt-oss-20b", "qwen/qwen3.6-27b"],
    "gemini": ["gemini-2.5-flash", "gemini-3.5-flash"],
    "claude_local": [CLAUDE_LOCAL_MODEL],
}

# Baseline configuration
EVAL_QUESTIONS_PATH = "baseline/questions.json"
ROUTING_PROBE_PATH = "baseline/routing_probe.json"  # routing-report question groups (no expected answers)

DB_PATH = os.path.join(LOCAL_DIR, "rag.db")

# Hybrid Search configuration
ENABLE_HYBRID_SEARCH = True
K_CANDIDATES = K_RETRIEVAL * 3

# Re-Ranker configuration
ENABLE_RERANKER = True
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

# Multi-Hop Agent configuration
ENABLE_MULTI_HOP = True
MAX_HOPS = 2

# Secret Provider configuration
SECRET_BACKEND = os.getenv("SECRET_BACKEND", "keyring")  # "keyring", "env", "azure_keyvault", "aws_secretsmanager"
KEYRING_SERVICE_NAME = os.getenv("KEYRING_SERVICE_NAME", "agentic-rag-platform")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Answer-context similarity below this logs a warning. A topic check, not a fact check. Measured on the benchmark
# (max over chunks): on-topic answers 0.35-0.79, bare numbers 0.18-0.28, other questions' answers 0.1-0.2 on average.
GROUNDEDNESS_THRESHOLD = float(os.getenv("GROUNDEDNESS_THRESHOLD", "0.3"))

# Azure Application Insights Telemetry
APPLICATIONINSIGHTS_CONNECTION_STRING = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")

# Azure Key Vault configuration
AZURE_KEYVAULT_URL = os.getenv("AZURE_KEYVAULT_URL", "")

# Persistent state snapshots
STATE_BACKEND = os.getenv("STATE_BACKEND", "local")  # "local", "azure_blob", "s3"
STATE_SNAPSHOT_NAME = os.getenv("STATE_SNAPSHOT_NAME", "state.tar.gz")
AZURE_STORAGE_ACCOUNT_URL = os.getenv("AZURE_STORAGE_ACCOUNT_URL", "")
AZURE_STATE_CONTAINER = os.getenv("AZURE_STATE_CONTAINER", "rag-state")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")  # local Azurite testing only
S3_STATE_BUCKET = os.getenv("S3_STATE_BUCKET", "")

# MCP server (HTTP transport). The SDK's DNS rebinding protection only accepts localhost Host headers,
# which is right for a local server; public deployments set this to false (main.bicep, deploy.yml).
MCP_DNS_REBINDING_PROTECTION = os.getenv("MCP_DNS_REBINDING_PROTECTION", "true").lower() in ("1", "true", "yes")

# Browser origins allowed to call the API (the web frontend), separated by spaces: deploy.yml's Lambda
# "Variables={...}" shorthand splits on commas. Empty allows none. Trailing slashes are dropped (browsers send none).
CORS_ALLOW_ORIGINS = [origin.rstrip("/") for origin in os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:3000").split()]

# Git SHA of the deployed build (deploy.yml sets it; empty locally), shown in /health and stamped on routing events
APP_REVISION = os.getenv("APP_REVISION", "")
# X-RAG-Client header value that marks live-eval's /query requests, so the routing stats can leave them out
LIVE_EVAL_CLIENT = "live-eval"
# Set by the AWS Lambda runtime. Lambda freezes the environment as soon as a response is sent, so work left for after
# the response (FastAPI background tasks) runs only when the next request thaws it: minutes later, or never.
ON_AWS_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

# Demo sandbox: public single-document Q&A, held in memory per instance and never persisted
DEMO_MAX_FILE_BYTES = 2 * 1024 * 1024
DEMO_MAX_TEXT_CHARS = 200_000
DEMO_TTL_SECONDS = 30 * 60  # idle time before a demo document is dropped
DEMO_MAX_DOCS = 20
DEMO_RATE_LIMIT_PER_MINUTE = 5

# Dashboard (/stats/*): each answer is reused this long, since computing it lists and reads the event store.
# Routing stats read at most the newest STATS_MAX_ROUTING_EVENTS events; eval history looks back this many days.
STATS_CACHE_SECONDS = 60
STATS_MAX_ROUTING_EVENTS = 2000
STATS_EVAL_HISTORY_DAYS = 90
