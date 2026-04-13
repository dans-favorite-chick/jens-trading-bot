"""
Phoenix Bot — Strategy Ingestor

Parses and indexes open-source trading strategies from academic
and community repositories into ChromaDB for RAG retrieval.

Supported sources:
- Papers With Backtest (140+ strategies)
- freqtrade community strategies
- Any Python file with strategy-like classes

Usage:
  python -m tools.strategy_ingestor --dir data/pwb-toolbox
  python -m tools.strategy_ingestor --dir data/freqtrade-strategies
  python -m tools.strategy_ingestor --list
"""

import ast
import json
import logging
import os
import sys
import argparse
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("StrategyIngestor")


class StrategyIngestor:
    """Parses Python strategy files and indexes into ChromaDB."""

    def __init__(self):
        self._collection = None
        self._init_db()

    def _init_db(self):
        """Initialize ChromaDB collection for strategy knowledge."""
        try:
            import chromadb
            db_path = os.path.join(
                os.path.dirname(__file__), "..", "data", "strategy_knowledge"
            )
            os.makedirs(db_path, exist_ok=True)
            client = chromadb.PersistentClient(path=db_path)
            self._collection = client.get_or_create_collection(
                name="strategy_library",
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(f"[INGESTOR] ChromaDB ready: {self._collection.count()} strategies indexed")
        except ImportError:
            logger.warning("[INGESTOR] chromadb not installed")
        except Exception as e:
            logger.warning(f"[INGESTOR] DB init failed: {e}")

    def ingest_directory(self, directory: str, source: str = "unknown") -> dict:
        """Walk a directory, parse all Python files for strategies."""
        if not os.path.isdir(directory):
            print(f"ERROR: Directory not found: {directory}")
            return {"parsed": 0, "indexed": 0, "errors": 0}

        stats = {"parsed": 0, "indexed": 0, "errors": 0, "skipped": 0}

        for root, dirs, files in os.walk(directory):
            # Skip hidden dirs, __pycache__, .git
            dirs[:] = [d for d in dirs if not d.startswith((".", "__"))]

            for fname in files:
                if not fname.endswith(".py"):
                    continue
                filepath = os.path.join(root, fname)
                try:
                    strategies = self._parse_file(filepath, source)
                    stats["parsed"] += 1
                    for strat in strategies:
                        if self._index_strategy(strat):
                            stats["indexed"] += 1
                        else:
                            stats["skipped"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    logger.debug(f"Parse error {filepath}: {e}")

        return stats

    def _parse_file(self, filepath: str, source: str) -> list:
        """Parse a Python file and extract strategy information."""
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        strategies = []

        try:
            tree = ast.parse(content)
        except SyntaxError:
            return strategies

        # Find classes that look like strategies
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue

            # Heuristic: it's a strategy if it has trading-related methods
            method_names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            trading_methods = {"should_enter", "populate_entry_trend", "populate_buy_trend",
                             "entry_signal", "generate_signal", "evaluate", "should_buy",
                             "should_sell", "calculate", "init", "next"}

            if not any(m in trading_methods for m in method_names):
                continue

            # Extract docstring
            docstring = ast.get_docstring(node) or ""

            # Extract class-level string assignments (name, description, etc.)
            name = node.name
            description = docstring[:500] if docstring else f"Strategy class: {name}"

            # Look for class attributes
            attrs = {}
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for target in item.targets:
                        if isinstance(target, ast.Name) and isinstance(item.value, (ast.Constant, ast.Str)):
                            val = item.value.value if hasattr(item.value, 'value') else str(item.value.s)
                            attrs[target.id] = str(val)[:200]

            # Extract relevant code (first 3000 chars)
            rel_path = os.path.relpath(filepath)
            raw_code = content[:3000]

            strategies.append({
                "name": attrs.get("name", name),
                "class_name": name,
                "source": source,
                "source_file": rel_path,
                "description": description,
                "methods": method_names,
                "attributes": attrs,
                "raw_code": raw_code,
                "parsed_at": datetime.now().isoformat(),
            })

        return strategies

    def _index_strategy(self, strat: dict) -> bool:
        """Index a parsed strategy into ChromaDB."""
        if not self._collection:
            return False

        # Check for duplicates
        strat_id = f"{strat['source']}_{strat['class_name']}"
        existing = self._collection.get(ids=[strat_id])
        if existing and existing["ids"]:
            return False  # Already indexed

        # Build searchable document
        doc = (
            f"Strategy: {strat['name']}\n"
            f"Source: {strat['source']}\n"
            f"Description: {strat['description']}\n"
            f"Methods: {', '.join(strat['methods'])}\n"
            f"Code preview: {strat['raw_code'][:1500]}"
        )

        try:
            self._collection.add(
                ids=[strat_id],
                documents=[doc],
                metadatas=[{
                    "name": strat["name"][:100],
                    "source": strat["source"],
                    "class_name": strat["class_name"],
                    "source_file": strat["source_file"][:200],
                    "method_count": len(strat["methods"]),
                }],
            )
            return True
        except Exception as e:
            logger.debug(f"Index error: {e}")
            return False

    def query(self, query: str, n_results: int = 5) -> list:
        """Query indexed strategies by natural language."""
        if not self._collection or self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(n_results, self._collection.count()),
        )

        entries = []
        if results and results["documents"]:
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                entries.append({
                    "name": meta.get("name", ""),
                    "source": meta.get("source", ""),
                    "class_name": meta.get("class_name", ""),
                    "relevance": round(1 - dist, 3),
                    "preview": doc[:300],
                })
        return entries

    def get_stats(self) -> dict:
        """Get indexing statistics."""
        if not self._collection:
            return {"available": False, "count": 0}

        return {
            "available": True,
            "count": self._collection.count(),
        }


def main():
    parser = argparse.ArgumentParser(description="Phoenix Strategy Ingestor")
    parser.add_argument("--dir", type=str, help="Directory to ingest strategies from")
    parser.add_argument("--source", type=str, default="community", help="Source label")
    parser.add_argument("--list", action="store_true", help="List indexed strategies")
    parser.add_argument("--query", type=str, help="Search indexed strategies")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    ingestor = StrategyIngestor()

    if args.list:
        stats = ingestor.get_stats()
        print(f"\nIndexed strategies: {stats['count']}")
        return

    if args.query:
        results = ingestor.query(args.query, n_results=10)
        print(f"\nSearch results for: '{args.query}'")
        for i, r in enumerate(results, 1):
            print(f"  {i}. [{r['source']}] {r['name']} (relevance={r['relevance']:.3f})")
            print(f"     {r['preview'][:100]}...")
        return

    if args.dir:
        print(f"\nIngesting strategies from: {args.dir}")
        stats = ingestor.ingest_directory(args.dir, source=args.source)
        print(f"\nResults:")
        print(f"  Files parsed: {stats['parsed']}")
        print(f"  Strategies indexed: {stats['indexed']}")
        print(f"  Skipped (duplicates): {stats['skipped']}")
        print(f"  Errors: {stats['errors']}")
        print(f"  Total in DB: {ingestor.get_stats()['count']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
