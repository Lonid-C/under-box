"""ICT claim scheduling and optional experimental retrieval controls."""
from __future__ import annotations

import os
import re
from collections import deque
from concurrent.futures import FIRST_COMPLETED, wait

from .academic import verification_phase
from .usage import UsageExecutor
from .verification_agents import agent_for

CLAIM_SECONDS = 90
FIRST_READ_BATCH = 4
READ_BATCH_SECONDS = 15


def enabled():
    return os.environ.get("UNDERBOX_ICT_PIPELINE_NEXT") == "1"


def parallel_enabled():
    return enabled() or os.environ.get("UNDERBOX_ICT_PARALLEL_AGENTS") == "1"


def run_agents(claims, workers, verify, receive, dispatched):
    """Fill free slots across categories, waiting only on selected dependencies."""
    indexed = list(enumerate(claims, 1))
    agents = {i: agent_for(c) for i, c in indexed}
    phases = {i: verification_phase(c) for i, c in indexed}
    dependencies = {
        i: {j for j, _ in indexed if j != i and (
            agents[j].id in agents[i].dependencies or
            phases[i] < 4 and phases[j] < phases[i]
        )}
        for i, _ in indexed
    }
    queues = {}
    for i, claim in indexed:
        queues.setdefault(agents[i].id, deque()).append((i, claim))
    preferred = ("education", "patent", "paper")
    rotation = deque([key for key in preferred if key in queues]
                     + [key for key in queues if key not in preferred])
    completed = set()
    running = {}
    with UsageExecutor(max_workers=workers) as pool:
        while rotation or running:
            for _ in range(len(rotation)):
                if len(running) >= workers:
                    break
                key = rotation.popleft()
                queue = queues[key]
                choice = next((item for item in queue if dependencies[item[0]] <= completed), None)
                if choice:
                    queue.remove(choice)
                    i, claim = choice
                    running[pool.submit(verify, i, dispatched(claim))] = i
                if queue:
                    rotation.append(key)
            if not running:
                if rotation:
                    raise ValueError("核验协作计划出现循环依赖")
                break
            if len(running) == workers or not any(
                dependencies[i] <= completed for key in rotation for i, _ in queues[key]
            ):
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in sorted(finished, key=running.__getitem__):
                    receive(future.result())
                    completed.add(running.pop(future))


_SECTION = re.compile(
    r"^(?:\s*#{1,6}\s*)?(?:教育(?:背景|经历)|校园经历|获奖(?:经历|情况)?|竞赛获奖|"
    r"科研(?:情况|成果)|发表论文|学术论文|论文|发明专利|专利|学术会议|会议|其他文章|"
    r"参与项目|项目经历|工作经历|实习经历|Publications|Education|Experience)\s*[:：]?\s*$", re.I
)
_LIST_START = re.compile(r"^\s*(?:\[\d+\]|\d+[.)、](?=\s|[\u3400-\u9fff])|[-•]\s)")


def resume_sections(text):
    """Keep even short/unheaded material and cut long lists only at record boundaries."""
    chunks, lines, length = [], [], 0
    for line in text.splitlines(keepends=True):
        if lines and (_SECTION.fullmatch(line.strip()) or
                      length >= 2600 and (_LIST_START.match(line) or not lines[-1].strip())):
            if block := "".join(lines).strip():
                chunks.append(block)
            lines, length = [], 0
        lines.append(line)
        length += len(line)
    if block := "".join(lines).strip():
        chunks.append(block)
    return chunks


def split_resume(text, llm):
    from .split import split_claims

    sections = resume_sections(text) if len(text) >= 2400 else [text]
    results = []
    seen = set()
    for section in sections:
        for claim in split_claims(section, llm, complete_section=True):
            raw = re.sub(r"\s+", "", claim.raw_text)
            key = (claim.category, raw, tuple(sorted(set(claim.elements))))
            if raw and raw in re.sub(r"\s+", "", section) and key not in seen:
                seen.add(key)
                results.append(claim.model_copy(update={"id": f"c{len(results) + 1:02d}"}))
    return results
