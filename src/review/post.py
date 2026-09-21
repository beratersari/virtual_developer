"""Post one inline MR/PR thread per parsed finding (aMIR-mini contract)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from src.logger import logger
from src.review.azure_threads import azure_thread_context, parse_azure_threads
from src.review.diffmap import parse_unified_diff
from src.review.findings import Finding
from src.review.gitdiff import head_sha, merge_base, resolve_workdir, unified_diff
from src.review.position import build_position_variants, format_discussion
from src.review.similarity import should_skip_similar_reply
from src.review.threads import ExistingThread, match_finding_thread, parse_finding_threads


def discussion_sha_attempts(
    *,
    merge: str,
    gitlab_base: str = "",
    gitlab_start: str = "",
) -> List[tuple[str, str]]:
    pairs: List[tuple[str, str]] = []
    merge = (merge or "").strip()
    gitlab_base = (gitlab_base or "").strip()
    gitlab_start = (gitlab_start or gitlab_base or "").strip()
    if merge:
        pairs.append((merge, merge))
    gitlab = (gitlab_base, gitlab_start or gitlab_base)
    if gitlab[0] and gitlab not in pairs:
        pairs.append(gitlab)
    return pairs


def post_inline_findings(
    *,
    findings: Sequence[Finding],
    workdir: Optional[object],
    target_branch: str,
    azure: bool,
    meta: Dict[str, Any],
) -> int:
    """Post file-line threads. Returns how many were posted or replied."""
    if not findings:
        return 0
    clone = resolve_workdir(workdir)
    if clone is None:
        logger.warning("review findings skip: no clone")
        return 0
    base = merge_base(clone, target_branch)
    if not base:
        logger.warning("review findings skip: no merge-base")
        return 0
    try:
        diffmap = parse_unified_diff(unified_diff(clone, base))
    except Exception as exc:
        logger.warning(f"review findings skip: diff failed: {exc}")
        return 0
    head = head_sha(clone)
    if azure:
        return _post_azure(findings, diffmap, meta)
    return _post_gitlab(findings, diffmap, meta, merge=base, head=head)


def _post_gitlab(
    findings: Sequence[Finding],
    diffmap: Any,
    meta: Dict[str, Any],
    *,
    merge: str,
    head: str,
) -> int:
    from src.gitlab.client import GitlabClient

    host = str(meta.get("gitlab_host") or "").strip()
    project = meta.get("gitlab_project_id") or meta.get("gitlab_project")
    iid = meta.get("gitlab_mr_iid")
    if not host or not project or not iid:
        logger.warning("review findings skip: missing GitLab MR coords")
        return 0
    client = GitlabClient(host=host)
    gitlab_base, gitlab_start, gitlab_head = client.get_mr_diff_refs(
        project=project, mr_iid=int(iid)
    )
    head_sha_val = (gitlab_head or head or "").strip()
    existing = _existing_gitlab(client, project, int(iid))
    used: set[str] = set()
    posted = 0
    for finding in findings:
        variants: List[dict] = []
        seen: List[dict] = []
        for base_sha, start_sha in discussion_sha_attempts(
            merge=merge, gitlab_base=gitlab_base, gitlab_start=gitlab_start
        ):
            for item in build_position_variants(
                finding,
                diffmap,
                base_sha=base_sha,
                start_sha=start_sha,
                head_sha=head_sha_val,
            ):
                if item in seen:
                    continue
                seen.append(item)
                variants.append(item)
        if not variants:
            logger.warning(
                f"review finding skip {finding.path}:{finding.start_line}: "
                "no GitLab position"
            )
            continue
        body = format_discussion(finding)
        if _reuse_or_reply_gitlab(client, project, int(iid), finding, body, existing, used):
            posted += 1
            continue
        ok = False
        for position in variants:
            if client.post_mr_discussion(
                project=project, mr_iid=int(iid), body=body, position=position
            ):
                ok = True
                break
        if ok:
            posted += 1
        else:
            logger.warning(
                f"review finding post failed {finding.path}:{finding.start_line}"
            )
    if posted:
        logger.info(f"review findings posted {posted} GitLab thread(s)")
    return posted


def _post_azure(
    findings: Sequence[Finding],
    diffmap: Any,
    meta: Dict[str, Any],
) -> int:
    from src.azure.client import AzureDevOpsClient

    host = str(meta.get("azure_host") or "").strip()
    collection = str(meta.get("azure_collection_url") or "").strip()
    project = meta.get("azure_project") or ""
    repository = meta.get("azure_repository_id") or meta.get("azure_repository") or ""
    iid = meta.get("azure_pr_id")
    if not (host or collection) or not project or not repository or not iid:
        logger.warning("review findings skip: missing Azure PR coords")
        return 0
    client = AzureDevOpsClient(host=host, collection_url=collection)
    first_iter, second_iter = client.pr_iteration_span(
        project=str(project), repository=repository, pr_id=int(iid)
    )
    existing = _existing_azure(client, str(project), repository, int(iid))
    used: set[str] = set()
    posted = 0
    for finding in findings:
        context = azure_thread_context(finding, diffmap)
        if not context:
            logger.warning(
                f"review finding skip {finding.path}:{finding.start_line}: "
                "no Azure position"
            )
            continue
        body = format_discussion(finding)
        if _reuse_or_reply_azure(
            client, str(project), repository, int(iid), finding, body, existing, used
        ):
            posted += 1
            continue
        if client.post_pr_file_thread(
            project=str(project),
            repository=repository,
            pr_id=int(iid),
            body=body,
            thread_context=context,
            first_iteration=first_iter,
            second_iteration=second_iter,
        ):
            posted += 1
        else:
            logger.warning(
                f"review finding post failed {finding.path}:{finding.start_line}"
            )
    if posted:
        logger.info(f"review findings posted {posted} Azure thread(s)")
    return posted


def _existing_gitlab(client: Any, project: Any, iid: int) -> List[ExistingThread]:
    try:
        return parse_finding_threads(client.list_mr_discussions(project=project, mr_iid=iid))
    except Exception as exc:
        logger.warning(f"review list GitLab discussions failed: {exc}")
        return []


def _existing_azure(
    client: Any, project: str, repository: Any, iid: int
) -> List[ExistingThread]:
    try:
        return parse_azure_threads(
            client.list_pr_threads(project=project, repository=repository, pr_id=iid)
        )
    except Exception as exc:
        logger.warning(f"review list Azure threads failed: {exc}")
        return []


def _reuse_or_reply_gitlab(
    client: Any,
    project: Any,
    iid: int,
    finding: Finding,
    body: str,
    existing: Sequence[ExistingThread],
    used: set[str],
) -> bool:
    matched = match_finding_thread(finding, existing, used)
    if matched is None:
        return False
    if should_skip_similar_reply(body, matched.last_body):
        used.add(matched.discussion_id)
        logger.info(f"review finding skip similar {matched.discussion_id}")
        return True
    posted = client.post_mr_note(
        project=project,
        mr_iid=iid,
        body=body,
        discussion_id=matched.discussion_id,
        allow_new_thread=False,
    )
    if posted is None:
        return False
    used.add(matched.discussion_id)
    return True


def _reuse_or_reply_azure(
    client: Any,
    project: str,
    repository: Any,
    iid: int,
    finding: Finding,
    body: str,
    existing: Sequence[ExistingThread],
    used: set[str],
) -> bool:
    matched = match_finding_thread(finding, existing, used)
    if matched is None:
        return False
    if should_skip_similar_reply(body, matched.last_body):
        used.add(matched.discussion_id)
        logger.info(f"review finding skip similar {matched.discussion_id}")
        return True
    posted = client.post_pr_comment(
        project=project,
        repository=repository,
        pr_id=iid,
        body=body,
        thread_id=matched.discussion_id,
        allow_new_thread=False,
        parent_comment_id=str(matched.root_comment_id or ""),
    )
    if posted is None:
        return False
    used.add(matched.discussion_id)
    return True
