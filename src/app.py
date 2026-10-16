import argparse
import os
import shutil
from dotenv import load_dotenv
from src.logging_config import setup_logging
from src.config import MAIN_LLM_MODEL, CHROMA_PERSIST_DIR
 
load_dotenv()
logger = setup_logging(__name__)

def main():
    parser = argparse.ArgumentParser(description="RAG System")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Index command
    index_parser = subparsers.add_parser("index", help="Build/update document index")
    index_parser.add_argument("--rebuild", action="store_true", help="Force rebuild of index")
    
    # Batch ingestion command
    batch_parser = subparsers.add_parser("ingest-batch",
                                         help="Index every file in an inbox folder and publish one snapshot")
    batch_parser.add_argument("--dir", dest="inbox_dir", default="inbox",
                              help="Folder with .txt, .md and .pdf files (default: inbox)")
    batch_parser.add_argument("--skip-questions", action="store_true",
                              help="Don't have the LLM write questions (offline runs); a later batch fills them in")
    batch_parser.add_argument("--result-file", default=None,
                              help="Also write the result as JSON (read by the scheduled workflow)")

    # Release-notes collector command
    collect_parser = subparsers.add_parser("collect-releases",
                                           help="Write a GitHub repo's newest releases into one inbox file for ingest-batch")
    collect_parser.add_argument("--repo", default="pydantic/pydantic-ai", help="owner/name (default: pydantic/pydantic-ai)")
    collect_parser.add_argument("--limit", type=int, default=10, help="Newest published releases to keep (default: 10)")
    collect_parser.add_argument("--dir", dest="inbox_dir", default="inbox", help="Inbox folder (default: inbox)")

    # Query command
    query_parser = subparsers.add_parser("query", help="Query the RAG system")
    query_parser.add_argument("question", help="Question to ask")
    
    # Evaluate command
    eval_parser = subparsers.add_parser("evaluate", help="Run evaluation tests")

    # API commands
    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI REST API server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
    serve_parser.add_argument("--port", type=int, default=8000, help="Port to bind (default: 8000)")

    history_parser = subparsers.add_parser("history", help="View past evaluation runs")
    history_parser.add_argument("--limit", type=int, default=10, help="Number of past runs to display")

    # Secret management commands
    set_secret_parser = subparsers.add_parser("set-secret", help="Set a secret in the OS keyring")
    set_secret_parser.add_argument("name", help="Secret name (e.g. GROQ_API_KEY)")
    set_secret_parser.add_argument("value", nargs="?", default=None, help="Secret value")
    get_secret_parser = subparsers.add_parser("get-secret", help="Get a secret from keyring / env")
    get_secret_parser.add_argument("name", help="Secret name (e.g. GROQ_API_KEY)")
        
    # Live evaluation command
    live_eval_parser = subparsers.add_parser("live-eval", help="Run evaluation against a live deployment")
    live_eval_parser.add_argument("--target-url", required=True, help="Target API URL (e.g. http://localhost:8000)")
    live_eval_parser.add_argument("--revision", default=None, help="Git SHA or release tag")
    live_eval_parser.add_argument("--questions", default=None,
                                  help="Question file in baseline/questions.json format, saved as a synthetic run "
                                       "(default: the benchmark, saved as live)")
    live_eval_parser.add_argument("--report-only", action="store_true",
                                  help="Exit 0 even when the score is below the defined threshold "
                                       "(the score is still logged and saved)")

    # Generated questions command
    questions_parser = subparsers.add_parser("questions", help="Work with the questions ingest-batch generated")
    questions_parser.add_argument("action", choices=["export"],
                                  help="export: write them in baseline/questions.json format, for live-eval --questions")
    questions_parser.add_argument("--out", required=True, help="JSON file to write")
    questions_parser.add_argument("--source", action="append", default=None,
                                  help="Only this document's questions (repeatable; default: all)")

    # State snapshot commands
    state_parser = subparsers.add_parser("state", help="Inspect or restore the persistent state snapshot")
    state_parser.add_argument("action", choices=["status", "pull"],
                              help="status: show versions; pull: restore the latest snapshot into the working dirs")

    # Routing report command
    routing_parser = subparsers.add_parser("routing-report",
                                           help="Show how the router decides on the benchmark and probe questions (no LLM calls)")
    routing_parser.add_argument("--probe-file", default=None,
                                help="Question groups to route (default: baseline/routing_probe.json)")

    args = parser.parse_args()

    if args.command == "index":
        import sys
        from src.core.indexing import reindex
        from src.storage.state_store import SnapshotConflict
        from src.storage.state_sync import StateReadOnlyError
        try:
            reindex(force_rebuild=args.rebuild)
        except StateReadOnlyError as e:
            print(f"Index not published: {e}")
            sys.exit(1)
        except SnapshotConflict as e:
            print(f"Index not published: {e}.\n"
                  "Your working copy is out of date. 'python -m src.app state pull' replaces DATA_DIR "
                  "with the latest snapshot, so save any new files first, then repeat your change.")
            sys.exit(1)
        logger.info("Index sync and ML model update completed.")

    elif args.command == "ingest-batch":
        import sys
        from src.config import STATE_BACKEND
        from src.ingestion.batch import RoutingRegression, ingest_batch
        from src.storage.state_store import SnapshotConflict
        from src.storage.state_sync import StateReadOnlyError
        try:
            result = ingest_batch(args.inbox_dir, generate=not args.skip_questions)
        except (FileNotFoundError, StateReadOnlyError) as e:
            print(f"Batch not ingested: {e}")
            sys.exit(1)
        except SnapshotConflict as e:
            print(f"Batch not published: {e}.\n"
                  "Another writer published while this batch ran. Run ingest-batch again; it starts from the latest snapshot.")
            sys.exit(1)
        except RoutingRegression as e:
            print(f"Batch not published: {e}. After the retrain, the router would send them elsewhere:")
            for line in e.failures:
                print(f"  {line}")
            sys.exit(1)

        for status, names in (("added", result.added), ("modified", result.modified), ("unchanged", result.unchanged)):
            for name in names:
                print(f"  {status:<10} {name}")
        for name, reason in result.failed:
            print(f"  {'FAILED':<10} {name}: {reason}")
        if result.questions_generated:
            print(f"  {'questions':<10} {result.questions_generated} written for {', '.join(result.question_sources)}")

        if not result.published:
            print("Nothing new to index.")
        elif STATE_BACKEND.lower() == "local":
            print(f"Indexed locally ({len(result.changed_sources)} sources changed); STATE_BACKEND=local publishes nothing.")
        else:
            print(f"Published snapshot {result.state_version} ({len(result.changed_sources)} sources changed).\n"
                  "Running apps keep serving the previous snapshot until restarted (README: 'Add documents').")
        if args.result_file:
            import dataclasses
            import json
            with open(args.result_file, "w", encoding="utf-8") as f:
                json.dump({**dataclasses.asdict(result), "changed_sources": sorted(result.changed_sources)}, f, indent=2)
        sys.exit(1 if result.failed else 0)

    elif args.command == "collect-releases":
        import sys
        import requests
        from src.collectors.github_releases import collect_releases
        from src.secrets import get_secret
        try:
            # Optional: unauthenticated GitHub API calls are limited to 60 per hour per IP
            path, count = collect_releases(args.repo, args.inbox_dir, args.limit, token=get_secret("GITHUB_TOKEN"))
        except (requests.RequestException, ValueError) as e:
            print(f"Releases not collected: {e}")
            sys.exit(1)
        print(f"Wrote {count} releases of {args.repo} to {path}")

    elif args.command == "query":
        from src.core.vectorstore import create_or_get_vectorstore
        from src.core.rag_agent import setup_router, answer_question
        from src.core.utils import create_llm
        router = setup_router()
        vectorstore = create_or_get_vectorstore()
        answer_llm = create_llm(MAIN_LLM_MODEL)
        result = answer_question(args.question, router, vectorstore, answer_llm)
        print(result.answer)
        if result.cache_hit:
            print("\n(semantic cache hit)")
        else:
            scores = {"confidence": result.router_confidence, "closest chunk": result.probe_similarity,
                      "answer-context similarity": result.groundedness_score}
            shown = "".join(f", {name} {value:.2f}" for name, value in scores.items() if value is not None)
            print(f"\n(source {result.source}, reason {result.route_reason}{shown})")
            
    elif args.command == "evaluate":
        from src.evaluation.evaluator import run_evaluation
        run_evaluation()

    elif args.command == "serve":
        if MAIN_LLM_MODEL == "claude_local" and args.host not in ("127.0.0.1", "localhost", "::1"):
            parser.error("MAIN_LLM_MODEL=claude_local answers with your own Claude subscription, so only this "
                         "machine may reach the server: add --host 127.0.0.1")
        import uvicorn
        logger.info(f"Starting API server on {args.host}:{args.port}")
        uvicorn.run("src.core.api:app", host=args.host, port=args.port, reload=False)

    elif args.command == "history":
        from src.evaluation.eval_db import get_eval_history
        from src.config import DB_PATH
        history = get_eval_history(DB_PATH, limit=args.limit)
        if not history:
            print("No evaluation runs found in history.")
        else:
            print(f"\n--- Last {len(history)} Evaluation Runs ---")
            print(f"{'ID':<4} {'Timestamp':<20} {'Type':<8} {'Model':<16} {'Score':<8} {'Passed':<6} {'Revision':<10}")
            print("-" * 80)
            for run in history:
                ts = run['timestamp'][:19].replace('T', ' ')
                score = f"{run['precision_score']:.1f}%"
                passed = "YES" if run['passed_threshold'] else "NO"
                run_type = run.get('run_type') or 'offline'
                rev = run.get('revision') or '-'
                print(f"{run['id']:<4} {ts:<20} {run_type:<8} {run['main_model']:<16} {score:<8} {passed:<6} {rev:<10}")   

    elif args.command == "set-secret":
        import keyring
        import getpass
        from src.config import KEYRING_SERVICE_NAME
        val = args.value or getpass.getpass(f"Enter secret value for '{args.name}': ") # to hide raw value
        keyring.set_password(KEYRING_SERVICE_NAME, args.name, val)
        logger.info(f"Secret '{args.name}' set successfully in OS Keyring ('{KEYRING_SERVICE_NAME}').")

    elif args.command == "get-secret":
        from src.secrets import get_secret
        val = get_secret(args.name)
        if val is not None:
            masked = val[:4] + "..." + val[-4:] if len(val) > 8 else "***"
            print(f"{args.name}: {masked}")
        else:
            print(f"Secret '{args.name}' not found.")
 
    elif args.command == "live-eval":
        import sys
        from src.config import EVAL_QUESTIONS_PATH
        from src.evaluation.live_evaluator import run_live_evaluation
        run_id, precision, passed = run_live_evaluation(
            args.target_url, args.revision,
            questions_path=args.questions or EVAL_QUESTIONS_PATH,
            run_type="synthetic" if args.questions else "live",
        )
        if not passed and not args.report_only:
            sys.exit(1)
        else:
            sys.exit(0)

    elif args.command == "questions":
        import json
        from src.evaluation.live_evaluator import synthetic_questions
        questions = synthetic_questions(args.source)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(questions, f, indent=2, ensure_ascii=False)
        print(f"Wrote {len(questions)} questions to {args.out}")

    elif args.command == "state":
        import sys
        from src.config import STATE_BACKEND, DATA_DIR, LOCAL_DIR
        from src.storage.state_store import get_state_store
        from src.storage.state_sync import initialize_state, read_local_version, read_only_reason

        if args.action == "pull":
            initialize_state()
            if read_only_reason():
                print(f"Pull failed: {read_only_reason()}")
                sys.exit(1)

        store = get_state_store()
        stored = (store.current_version() or "-") if store else "n/a (local backend)"
        print(f"Backend:        {STATE_BACKEND}")
        print(f"Data dir:       {DATA_DIR}")
        print(f"Local dir:      {LOCAL_DIR}")
        print(f"Local version:  {read_local_version() or '-'}")
        print(f"Stored version: {stored}")

    elif args.command == "routing-report":
        import logging
        from src.config import ROUTING_PROBE_PATH
        from src.core.vectorstore import create_or_get_vectorstore
        from src.evaluation.routing_report import format_report, load_probe_questions, run_routing_report
        # One table instead of 3-4 router log lines per question (set after the import, which configures the logger)
        logging.getLogger("src.routing.router").setLevel(logging.WARNING)
        items = load_probe_questions(probe_path=args.probe_file or ROUTING_PROBE_PATH)
        print(format_report(run_routing_report(create_or_get_vectorstore(), items)))

    else:
        parser.print_help()

if __name__ == "__main__":
    main()