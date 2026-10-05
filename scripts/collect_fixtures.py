"""Collect redacted console and JUnit fixtures from a Jenkins build.

Owner: Shared. Writes fixture files under tests/jenkins_agent/fixtures by default.
"""

import argparse
import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
import re
import sys

from dotenv import load_dotenv


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agents.jenkins_agent.tools.jenkins_client import JenkinsClient, JenkinsError
from common.redaction import redact


logger = logging.getLogger(__name__)
DEFAULT_FIXTURE_DIR = REPOSITORY_ROOT / "tests" / "jenkins_agent" / "fixtures"


@dataclass(frozen=True, slots=True)
class CollectedFixtures:
    console_path: Path
    junit_path: Path | None
    truncated: bool
    partial: bool


def fixture_stem(job: str, build_number: int) -> str:
    safe_job = re.sub(r"[^A-Za-z0-9._-]+", "_", job.strip("/"))
    safe_job = safe_job.strip("._-")[:100] or "job"
    return f"{safe_job}-build-{build_number}"


async def collect_fixtures(
    job: str,
    build_number: int,
    output_dir: Path = DEFAULT_FIXTURE_DIR,
) -> CollectedFixtures:
    """Download a build's console and optional JUnit XML into the fixture directory."""
    if build_number < 0:
        raise ValueError("build number must be non-negative")

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = fixture_stem(job, build_number)
    async with JenkinsClient() as client:
        console = await client.get_console_text(job, build_number)
        junit_xml = await client.get_test_report_xml(job, build_number)

    console_path = output_dir / f"{stem}.log"
    console_path.write_text(redact(console.text), encoding="utf-8")

    junit_path = None
    if junit_xml is not None:
        junit_path = output_dir / f"{stem}-junit.xml"
        junit_path.write_text(redact(junit_xml), encoding="utf-8")

    return CollectedFixtures(
        console_path=console_path,
        junit_path=junit_path,
        truncated=console.truncated,
        partial=console.partial,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", help="Jenkins job path, such as payments/main")
    parser.add_argument("build_number", type=int, help="Jenkins build number")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_FIXTURE_DIR,
        help=f"fixture output directory (default: {DEFAULT_FIXTURE_DIR})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv(REPOSITORY_ROOT / ".env")
    args = _argument_parser().parse_args(argv)
    try:
        result = asyncio.run(collect_fixtures(args.job, args.build_number, args.output_dir))
    except (JenkinsError, OSError, ValueError) as error:
        logger.error("Could not collect Jenkins fixtures: %s", error)
        return 1

    logger.info(
        "Saved console fixture %s (truncated=%s, partial=%s)",
        result.console_path,
        result.truncated,
        result.partial,
    )
    if result.junit_path is None:
        logger.info("No JUnit report exists for this build; skipped XML fixture")
    else:
        logger.info("Saved JUnit fixture %s", result.junit_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())