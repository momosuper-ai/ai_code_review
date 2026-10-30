"""Evidence validation, deterministic rendering, and immutable artifacts."""

import hashlib
import json
import os
import posixpath
import shutil
import zipfile
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4

from filelock import FileLock

from src.config import settings
from src.core.project_brain import GENERATOR_VERSION
from src.core.project_brain.compiler import encoded
from src.core.project_brain.schema import BrainManifest, Claim, Compilation, FactBundle, KnowledgeCard, stable_id
from src.core.project_brain.public_schema import (
    PublicBrainManifest, PublicBundle, PublicKnowledgeCard, PublicSourceRef, parse_manifest,
)
from src.core.project_brain.snapshot import RepoSnapshot, safe_relative


def owned_path(path: str) -> bool:
    """目录级写入边界；内部文件名和扩展名不参与授权判断。"""
    safe_relative(path)
    return path == "PROJECT_BRAIN.md" or path.startswith("docs/project-brain/")


class ArtifactStore:
    """统一路径归属和不可变版本，API 仅使用只读方法。"""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or settings.PROJECT_BRAIN_BASE_DIR).resolve()
        self.work_base = Path(settings.PROJECT_BRAIN_WORK_DIR if base_dir is None and settings.PROJECT_BRAIN_WORK_DIR
                              else self.base / "_work").resolve()

    def directory(self, build_id: str, area: str = "builds") -> Path:
        """只允许服务生成的 UUID 路径。"""
        UUID(build_id)
        if area not in {"builds", "work", "snapshots"}:
            raise ValueError("产物区域无效")
        root = (self.base if area == "builds" else self.work_base) / area
        result = (root / build_id).resolve()
        if not result.is_relative_to(root.resolve()) or result == root.resolve():
            raise ValueError("产物目录越界")
        return result

    def remove(self, directory: Path) -> None:
        """清理经过绝对路径归属检查的服务目录。"""
        resolved = directory.resolve()
        roots = [self.base / "builds", self.work_base / "work", self.work_base / "snapshots"]
        if directory.is_symlink() or not any(resolved.is_relative_to(root.resolve()) and resolved != root.resolve() for root in roots):
            raise ValueError("拒绝清理越界目录")
        if resolved.exists():
            shutil.rmtree(resolved, onerror=lambda fn, p, info: (os.chmod(p, 0o600), fn(p)))

    def write_work(self, build_id: str, name: str, data: object) -> None:
        """原子写入不含原始上下文的阶段检查点。"""
        safe_relative(name)
        root = self.directory(build_id, "work")
        root.mkdir(parents=True, exist_ok=True)
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        temporary = dest.with_name(dest.name + "." + uuid4().hex)
        temporary.write_text(encoded(data), encoding="utf-8")
        temporary.replace(dest)

    def read_work(self, build_id: str, name: str) -> dict:
        """读取结构化阶段检查点。"""
        safe_relative(name)
        return json.loads((self.directory(build_id, "work") / name).read_text(encoding="utf-8"))

    def read_manifest(self, build_id: str) -> BrainManifest | PublicBrainManifest:
        """读取并验证就绪清单。"""
        root = self.directory(build_id)
        return parse_manifest((root / "repository/docs/project-brain/manifest.json").read_text(encoding="utf-8"))

    def read_cards(self, build_id: str) -> list[KnowledgeCard]:
        """读取当前不可变版本的卡片。"""
        root = self.directory(build_id)
        self.verify_service_files(build_id, names={"cards.json"})
        values = json.loads((root / "cards.json").read_text(encoding="utf-8"))
        model = PublicKnowledgeCard if isinstance(self.read_manifest(build_id), PublicBrainManifest) else KnowledgeCard
        return [model.model_validate(card) for card in values]

    def read_bundle(self, build_id: str) -> FactBundle:
        """读取用于增量重用的事实和提取检查点。"""
        self.verify_service_files(build_id, names={"facts.json"})
        if isinstance(self.read_manifest(build_id), PublicBrainManifest):
            raise ValueError("公开知识不能作为规范提取缓存")
        return FactBundle.model_validate_json((self.directory(build_id) / "facts.json").read_text(encoding="utf-8"))

    def read_public_bundle(self, build_id: str) -> PublicBundle:
        """Read only schema 2 safe graph; it is never a parser checkpoint."""
        self.verify_service_files(build_id, names={"facts.json"})
        if not isinstance(self.read_manifest(build_id), PublicBrainManifest):
            raise ValueError("旧知识没有独立公开Schema")
        return PublicBundle.model_validate_json((self.directory(build_id) / "facts.json").read_text(encoding="utf-8"))

    def verify_service_files(self, build_id: str, names: set[str] | None = None) -> None:
        """Verify new manifests' complete service-side graph/card checksums.

        Old manifests have no service checksums and retain legacy readability;
        callers must not report them as checksum-verified service-side exports.
        """
        manifest = self.read_manifest(build_id)
        if isinstance(manifest, PublicBrainManifest) and set(manifest.service_files) != {
                "cards.json", "facts.json", "symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json"}:
            raise ValueError("公开服务侧知识产物未列入完整校验清单")
        root = self.directory(build_id)
        for name, digest in manifest.service_files.items():
            safe_relative(name)
            if names is not None and name not in names:
                continue
            full = root / name
            if (full.is_symlink() or not full.resolve().is_relative_to(root.resolve())
                    or hashlib.sha256(full.read_bytes()).hexdigest() != digest):
                raise ValueError("服务侧知识产物哈希或路径无效")
        if manifest.service_files and names is not None and not names <= manifest.service_files.keys():
            raise ValueError("服务侧知识产物未列入校验清单")

    def export_path(self, build_id: str, expected_sha256: str | None = None) -> Path:
        """Check the immutable files and recorded archive before downloading."""
        self.repository(build_id)
        self.verify_service_files(build_id)
        archive = self.directory(build_id) / "knowledge.zip"
        if archive.is_symlink() or not archive.is_file():
            raise ValueError("知识导出路径无效")
        if expected_sha256:
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected_sha256:
                    raise ValueError("知识导出 ZIP 哈希无效")
        return archive

    def repository(self, build_id: str) -> dict[str, str]:
        """核对清单哈希后返回 GitLab 可发布内容。"""
        manifest = self.read_manifest(build_id)
        root = self.directory(build_id) / "repository"
        result = {}
        for path, digest in manifest.files.items():
            if not owned_path(path):
                raise ValueError("产物清单包含非法发布路径")
            full = root / path
            if not full.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [full, *full.parents] if p != root.parent):
                raise ValueError("知识产物包含符号链接或越界目标")
            data = full.read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError("产物哈希不匹配")
            result[path] = data.decode("utf-8")
        result["docs/project-brain/manifest.json"] = (root / "docs/project-brain/manifest.json").read_text(encoding="utf-8")
        return result

    def finalize(self, manifest: BrainManifest | PublicBrainManifest, bundle: FactBundle | PublicBundle,
                 cards: list[KnowledgeCard], project_url: str) -> Path:
        """完整写入暂存产物及 ZIP 后原子发布；重试复用相同构建。"""
        if isinstance(bundle, PublicBundle):
            from src.core.project_brain.public_projection import verify_projection
            verify_projection(bundle, cards, manifest)
        elif getattr(bundle, "source_identity", None) is not None or isinstance(manifest, PublicBrainManifest):
            raise ValueError("规范来源必须经独立公开投影后才能封存")
        destination = self.directory(manifest.build_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(destination) + ".lock"):
            if destination.exists():
                existing = self.read_manifest(manifest.build_id)
                if (existing.source_commit != manifest.source_commit or existing.recipe_hash != manifest.recipe_hash
                        or existing.schema_version != manifest.schema_version
                        or isinstance(existing, PublicBrainManifest) and existing.projection.content_digest != manifest.projection.content_digest):
                    raise ValueError("不可变版本与构建不一致")
                self.repository(manifest.build_id)
                self.verify_service_files(manifest.build_id)
                return destination
            staging = destination.with_name(destination.name + ".tmp-" + uuid4().hex)
            staging.mkdir()
            try:
                repository = render_repository(cards, manifest, bundle, project_url)
                manifest.card_paths = {card.id: card_path(card) for card in cards}
                manifest.files = {path: hashlib.sha256(data.encode()).hexdigest() for path, data in repository.items()}
                for path, data in repository.items():
                    if not owned_path(path):
                        raise ValueError("非法渲染路径")
                    full = staging / "repository" / path
                    full.parent.mkdir(parents=True, exist_ok=True)
                    full.write_text(data, encoding="utf-8", newline="\n")
                (staging / "cards.json").write_text(encoded([c.model_dump() for c in cards]), encoding="utf-8")
                (staging / "facts.json").write_text(bundle.model_dump_json(), encoding="utf-8")
                for name, values in (("symbols.jsonl", bundle.facts), ("relations.jsonl", bundle.relations), ("source_refs.jsonl", bundle.source_refs)):
                    (staging / name).write_text("\n".join(v.model_dump_json() for v in values) + "\n", encoding="utf-8")
                (staging / "index.json").write_text(encoded({c.id: {"kind": c.kind, "module_id": c.module_id, "title": c.title, "path": card_path(c)} for c in cards}), encoding="utf-8")
                manifest.service_files = {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                                          for name in ("cards.json", "facts.json", "symbols.jsonl", "relations.jsonl", "source_refs.jsonl", "index.json")}
                (staging / "repository/docs/project-brain/manifest.json").write_text(encoded(manifest.model_dump()) + "\n", encoding="utf-8", newline="\n")
                # Only named public files belong to the archive. A temporary
                # private file accidentally placed nearby must never be swept
                # into an export by recursive directory enumeration.
                members = [*("repository/" + path for path in repository),
                           "repository/docs/project-brain/manifest.json", *manifest.service_files]
                with zipfile.ZipFile(staging / "knowledge.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for name in sorted(members):
                        safe_relative(name)
                        full = staging / name
                        if full.is_symlink() or not full.is_file() or not full.resolve().is_relative_to(staging.resolve()):
                            raise ValueError("知识导出成员无效")
                        archive.write(full, name)
                allowed = {*members, "knowledge.zip"}
                for full in staging.rglob("*"):
                    name = full.relative_to(staging).as_posix()
                    if full.is_symlink() or full.is_file() and name not in allowed:
                        raise ValueError("正式知识目录包含非公开清单文件")
                staging.rename(destination)
                return destination
            except BaseException:
                self.remove(staging)
                raise


def validate(bundle: FactBundle, cards: list[KnowledgeCard], snapshot: RepoSnapshot) -> dict:
    """验证证据、blob、实体关系与完整卡片，任何无效引用均阻止就绪。"""
    facts, refs, relations = ({f.id: f for f in bundle.facts}, {r.id: r for r in bundle.source_refs}, {r.id: r for r in bundle.relations})
    if len(facts) != len(bundle.facts) or len(refs) != len(bundle.source_refs) or len(relations) != len(bundle.relations):
        raise ValueError("事实或证据 ID 重复")
    for path, metadata in snapshot.files.items():
        safe_relative(path)
        if metadata["available"]:
            resolver = getattr(snapshot, "resolve_source", None)
            full = Path(resolver(path)) if resolver else Path(snapshot.path) / path
            if full.is_symlink() or not full.resolve().is_relative_to(Path(snapshot.path).resolve()):
                raise ValueError("源码快照存在越界路径")
            content = full.read_bytes()
            raw = f"blob {len(content)}\0".encode() + content
            digest = hashlib.sha1(raw).hexdigest() if len(metadata["blob_sha"]) == 40 else hashlib.sha256(raw).hexdigest()
            if digest != metadata["blob_sha"]:
                raise ValueError("快照内容与 Git blob 不一致")
    for ref in refs.values():
        metadata = snapshot.files.get(ref.path)
        if (not metadata or not metadata["available"] or ref.commit_sha != bundle.source_commit
                or metadata["blob_sha"] != ref.blob_sha or ref.line_end > metadata["lines"]
                or (ref.symbol_id and ref.symbol_id not in facts)):
            raise ValueError("源码证据无效")
    for fact in facts.values():
        if not fact.source_ref_ids or not set(fact.source_ref_ids) <= refs.keys():
            raise ValueError("事实缺少有效证据")
    for relation in relations.values():
        if (relation.from_id not in facts or (relation.to_id and relation.to_id not in facts)
                or not set(relation.candidates) <= facts.keys() or not relation.source_ref_ids
                or not set(relation.source_ref_ids) <= refs.keys()
                or (relation.resolution == "resolved") != bool(relation.to_id)
                or (relation.resolution == "candidate" and not relation.candidates)):
            raise ValueError("关系声明无效")
    ids = {c.id for c in cards}
    if len(ids) != len(cards):
        raise ValueError("卡片 ID 重复")
    for card in cards:
        if card.source_commit != bundle.source_commit or not set(card.links) <= ids or not card.claims:
            raise ValueError("卡片版本、导航或结论无效")
        for claim in [*card.claims, *card.detail_claims]:
            if not set(claim.evidence_ids) <= refs.keys() or not set(claim.relation_ids) <= relations.keys():
                raise ValueError("卡片引用无效")
            for rid in claim.relation_ids:
                r = relations[rid]
                if r.resolution != "resolved" and claim.knowledge_type == "DERIVED":
                    raise ValueError("候选关系不能冒充确定路径")
            scope = claim.source_scope
            if scope:
                if (claim.knowledge_type != "AI_INFERRED" or set(scope.source_ref_ids) != set(claim.evidence_ids)
                        or not set(scope.relation_ids) <= relations.keys()):
                    raise ValueError("推断来源范围或知识类型无效")
                if scope.kind == "declaration_fragment":
                    if scope.fact_id not in facts or any(relations[rid].from_id != scope.fact_id for rid in scope.relation_ids):
                        raise ValueError("推断来源主体或关系分片无效")
                    expected = set(facts[scope.fact_id].source_ref_ids)
                    expected.update(rid for relation_id in scope.relation_ids for rid in relations[relation_id].source_ref_ids)
                    if expected != set(scope.source_ref_ids):
                        raise ValueError("推断来源范围包含无关证据")
                elif scope.fact_id is not None or scope.relation_ids:
                    raise ValueError("汇总来源范围不能伪造声明主体")
                elif scope.kind == "summary_synthesis":
                    modules = getattr(scope, "module_ids", [])
                    children = getattr(scope, "child_ids", [])
                    if (not modules or len(modules) != len(set(modules))
                            or not set(modules) <= bundle.modules.keys()
                            or not children or len(children) != len(set(children))
                            or any(type(value) is not str or not value.strip() for value in children)):
                        raise ValueError("分层汇总来源覆盖无效")
    return {"files": len(bundle.files), "available_files": sum(bool(m["available"]) for m in bundle.files.values()),
            "facts": len(facts), "relations": len(relations), "resolved_relations": sum(r.resolution == "resolved" for r in relations.values()),
            "evidence": len(refs), "cards": len(cards), "valid_reference_ratio": 1.0,
            "evidence_coverage_ratio": 1.0, "source_reference_coverage_ratio": 1.0,
            "coverage_metric_meaning": "引用存在和输入范围有效性，不是业务语义支持率",
            "semantic_support_status": "unreviewed", "limitations": len(bundle.issues)}


def assemble_cards(bundle: FactBundle, compiled: list[tuple[str, Compilation]], summaries: list[Compilation],
                   *, hierarchy: dict | None = None) -> list[KnowledgeCard]:
    """将全部编译单元与事实确定性组装，删除的模块不会从旧版本继承。"""
    display_paths = getattr(bundle, "display_paths", {})
    module_names = {module: display_paths.get(locator, locator) for module, locator in bundle.modules.items()}
    facts = {f.id: f for f in bundle.facts}
    by_module: dict[str, list[Claim]] = defaultdict(list)
    unknowns: dict[str, list[str]] = defaultdict(list)
    paths: dict[str, str] = {}
    for module, compilation in compiled:
        by_module[module].extend(Claim(text=c.text, knowledge_type="AI_INFERRED", evidence_ids=c.evidence_ids,
                                      source_scope=c.source_scope) for c in compilation.claims)
        unknowns[module].extend(compilation.unknowns)
        if compilation.document_path and module not in paths:
            paths[module] = compilation.document_path
    def inferred(compilation: Compilation) -> list[Claim]:
        return [Claim(text=c.text, knowledge_type="AI_INFERRED", evidence_ids=c.evidence_ids,
                      source_scope=c.source_scope) for c in compilation.claims]

    expected_modules = {fact.module_id for fact in bundle.facts}
    empty_module_gaps = [f"模块 {module_names[module]} 未提取可编译事实，未生成语义概览。"
                         for module in sorted(set(bundle.modules) - expected_modules)]
    if hierarchy is not None:
        required = {"modules", "project", "intermediates", "all_unknowns", "coverage"}
        if not isinstance(hierarchy, dict) or set(hierarchy) != required:
            raise ValueError("分层汇总组装信息无效")
        coverage = hierarchy["coverage"]
        covered_modules = coverage.get("module_ids", []) if isinstance(coverage, dict) else []
        leaves = coverage.get("leaf_ids", []) if isinstance(coverage, dict) else []
        if (set(hierarchy["modules"]) != expected_modules or set(by_module) != expected_modules
                or not isinstance(covered_modules, list) or len(covered_modules) != len(set(covered_modules))
                or set(covered_modules) != expected_modules or not isinstance(leaves, list)
                or not leaves or len(leaves) != len(set(leaves))
                or any(type(value) is not str or not value.strip() for value in leaves)
                or type(coverage.get("root")) is not str or not coverage["root"].strip()):
            raise ValueError("分层汇总未覆盖完整模块与编译单元")
        if not set(hierarchy["intermediates"]) <= expected_modules | {"project"}:
            raise ValueError("分层汇总中间结果归属无效")
        if not set(hierarchy["all_unknowns"]) <= expected_modules | {"project"}:
            raise ValueError("分层汇总未知项归属无效")
        overviews = {module: Compilation.model_validate(value)
                     for module, value in hierarchy["modules"].items()}
        project_overview = Compilation.model_validate(hierarchy["project"])
        intermediates = {module: [Compilation.model_validate(value) for value in values]
                         for module, values in hierarchy["intermediates"].items()}
        for module, overview in overviews.items():
            for claim in overview.claims:
                if (not claim.source_scope or claim.source_scope.kind != "summary_synthesis"
                        or set(getattr(claim.source_scope, "module_ids", [])) != {module}):
                    raise ValueError("模块概览来源归属无效")
        for claim in project_overview.claims:
            if (not claim.source_scope or claim.source_scope.kind != "summary_synthesis"
                    or set(getattr(claim.source_scope, "module_ids", [])) != expected_modules):
                raise ValueError("项目概览未覆盖完整模块集合")
        cards = [KnowledgeCard(
            id=module, kind="module", title=module_names[module], module_id=module,
            source_commit=bundle.source_commit, claims=inferred(overviews[module]),
            detail_claims=by_module[module] + [claim for value in intermediates.get(module, [])
                                              for claim in inferred(value)],
            unknowns=sorted(set(bundle.issues + overviews[module].unknowns)),
            detail_unknowns=sorted(set(unknowns[module] + hierarchy["all_unknowns"].get(module, [])
                                      + [u for value in intermediates.get(module, []) for u in value.unknowns])),
            document_path=overviews[module].document_path or paths.get(module),
        ) for module in sorted(expected_modules)]
    else:
        cards = [KnowledgeCard(id=m, kind="module", title=module_names[m], module_id=m, source_commit=bundle.source_commit,
                               claims=claims, unknowns=sorted(set(unknowns[m])), document_path=paths.get(m)) for m, claims in sorted(by_module.items())]
    first_refs = bundle.facts[0].source_ref_ids
    project_claims = [Claim(text="源码模块：" + name, knowledge_type="FACT",
                            evidence_ids=next(f.source_ref_ids for f in bundle.facts if f.module_id == module))
                      for module, name in sorted(module_names.items()) if module in expected_modules]
    project_claims.extend(inferred(project_overview) if hierarchy is not None
                          else [claim for value in summaries for claim in inferred(value)])
    project = KnowledgeCard(id=stable_id("project", "root"), kind="project", title="项目知识导航", module_id=None,
                            source_commit=bundle.source_commit, claims=project_claims, links=[c.id for c in cards],
                            unknowns=sorted(set(bundle.issues + empty_module_gaps + (project_overview.unknowns if hierarchy is not None
                                else [u for compilation in [c for _, c in compiled] + summaries for u in compilation.unknowns]))),
                            detail_claims=([claim for value in intermediates.get("project", []) for claim in inferred(value)]
                                           if hierarchy is not None else []),
                            detail_unknowns=(sorted(set([u for _, value in compiled for u in value.unknowns]
                                + [u for value in overviews.values() for u in value.unknowns]
                                + [u for values in intermediates.values() for value in values for u in value.unknowns]
                                + [u for values in hierarchy["all_unknowns"].values() for u in values]))
                                if hierarchy is not None else []))
    architecture_claims = []
    for r in bundle.relations:
        if r.resolution == "resolved" and facts[r.from_id].module_id != facts[r.to_id].module_id:
            architecture_claims.append(Claim(text=f"{module_names[facts[r.from_id].module_id]} → {module_names[facts[r.to_id].module_id]}（{r.type}）",
                                             knowledge_type="DERIVED", evidence_ids=r.source_ref_ids, relation_ids=[r.id]))
    architecture = KnowledgeCard(id=stable_id("architecture", "root"), kind="architecture", title="架构与依赖边界", module_id=None,
                                 source_commit=bundle.source_commit, claims=architecture_claims or [Claim(text="静态分析未确认跨模块依赖；不能据此断言模块完全独立。", knowledge_type="DERIVED", evidence_ids=first_refs)],
                                 links=[c.id for c in cards], unknowns=bundle.issues)
    cards.extend([project, architecture])
    adjacency: dict[str, list] = defaultdict(list)
    for r in bundle.relations:
        if r.type in {"CALLS", "HANDLED_BY", "IMPLEMENTED_BY", "READS_TABLE", "WRITES_TABLE"}:
            adjacency[r.from_id].append(r)
    for fact in bundle.facts:
        if fact.kind not in {"interface", "table"}:
            continue
        kind = "interface" if fact.kind == "interface" else "data"
        cid = stable_id(kind + "-card", fact.id)
        claims = [Claim(text=f"源码声明：{fact.name}", knowledge_type="FACT", evidence_ids=fact.source_ref_ids)]
        for r in bundle.relations:
            if (r.from_id == fact.id or r.to_id == fact.id) and r.resolution == "resolved":
                claims.append(Claim(text=f"{facts[r.from_id].name} —{r.type}→ {facts[r.to_id].name}", knowledge_type="FACT", evidence_ids=r.source_ref_ids, relation_ids=[r.id]))
        gaps = [f"{facts[r.from_id].name} 的 {r.type} 为 {r.resolution}，未确认为确定关系。" for r in bundle.relations
                if r.resolution != "resolved" and (fact.id in r.candidates or (kind == "data" and r.target_name == fact.name))]
        cards.append(KnowledgeCard(id=cid, kind=kind, title=fact.name, module_id=fact.module_id,
                                   source_commit=bundle.source_commit, claims=claims, links=[fact.module_id], unknowns=sorted(set(gaps))))
        if kind != "interface":
            continue
        queue, visited, discovered, flow_claims, gaps = deque([(fact.id, 0)]), set(), {fact.id}, [], []
        while queue and len(visited) < 100:
            node, depth = queue.popleft()
            if node in visited:
                gaps.append("发现循环或合流，路径只展开一次")
                continue
            visited.add(node)
            for r in adjacency[node]:
                if r.resolution != "resolved":
                    gaps.append(f"{facts[node].name} 的 {r.target_name} 为 {r.resolution}，未展开")
                    continue
                if r.to_id not in discovered and len(discovered) >= 100:
                    gaps.append("达到 100 节点展开上限")
                    continue
                discovered.add(r.to_id)
                flow_claims.append(Claim(text=f"第 {depth + 1} 层：{facts[node].name} —{r.type}→ {facts[r.to_id].name}",
                                         knowledge_type="DERIVED", evidence_ids=r.source_ref_ids, relation_ids=[r.id]))
                if depth < 4:
                    queue.append((r.to_id, depth + 1))
                elif adjacency[r.to_id]:
                    gaps.append("达到 5 层展开上限")
        if queue:
            gaps.append("达到 100 节点展开上限")
        flow_evidence = {rid for fid in discovered for rid in facts[fid].source_ref_ids}
        flow_evidence.update(rid for claim in flow_claims for rid in claim.evidence_ids)
        interpretations = [claim for values in by_module.values() for claim in values
                           if claim.source_scope is None and set(claim.evidence_ids) <= flow_evidence]
        if not interpretations:
            gaps.append("当前证据不足以生成路径专属业务解释，参见关联模块卡。")
        module_links = sorted({facts[fid].module_id for fid in discovered})
        cards.append(KnowledgeCard(id=stable_id("flow-card", fact.id), kind="flow", title=fact.name + "：静态执行路径", module_id=fact.module_id,
                                   source_commit=bundle.source_commit, claims=(flow_claims or claims[:1]) + interpretations, links=[cid, *module_links],
                                   unknowns=sorted(set(gaps + ["仅表示已确认的静态关系；调用顺序、条件分支和运行时行为未证明。"]))))
    return cards


def card_path(card: KnowledgeCard) -> str:
    """实体 ID 与文档路径独立；默认布局可由编译结果建议的位置替代。"""
    if card.document_path:
        if not owned_path(card.document_path) or not card.document_path.startswith("docs/project-brain/"):
            raise ValueError("卡片文档位置越界")
        return card.document_path
    if card.kind == "project":
        return "PROJECT_BRAIN.md"
    if card.kind == "architecture":
        return "docs/project-brain/architecture.md"
    folders = {"module": "modules", "interface": "interfaces", "data": "data", "flow": "flows"}
    return f"docs/project-brain/{folders[card.kind]}/{card.id.split(':')[-1]}.md"


def render_card(card: KnowledgeCard, manifest: BrainManifest, bundle: FactBundle, project_url: str, cards: list[KnowledgeCard]) -> str:
    """从规范 JSON 渲染固定提交链接及事实/推断标签。"""
    refs = {r.id: r for r in bundle.source_refs}
    all_cards = {c.id: c for c in cards}
    def escape(value: str) -> str:
        return value.replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")
    rows = ["<!-- project-brain:owned -->", f"# {escape(card.title)}", "",
            f"来源提交：`{card.source_commit}` · 知识版本：{manifest.brain_version} · 构建：`{manifest.build_id}`", "",
            "覆盖：静态事实及 AI 推断；动态行为和未适配框架见限制说明。", ""]
    def render_claims(claims: list[Claim]) -> list[str]:
        result = []
        for claim in claims:
            result.append(f"- **{claim.knowledge_type}**：{escape(claim.text)}")
            links = []
            for rid in dict.fromkeys(claim.evidence_ids):
                ref = refs[rid]
                if isinstance(ref, PublicSourceRef):
                    label = f"{escape(ref.display_path)}:{ref.line_start}-{ref.line_end}"
                    if ref.source_url:
                        links.append(f"[{label}]({ref.source_url})")
                    else:
                        state = "路径已脱敏；来源已由服务校验" if ref.path_redacted else "来源已由服务校验；未提供安全直链"
                        links.append(f"{label}（{state}；证据 `{ref.id}`；blob `{ref.blob_sha}`）")
                else:
                    url = f"{project_url.rstrip('/')}/-/blob/{ref.commit_sha}/{quote(ref.path, safe='/')}#L{ref.line_start}-{ref.line_end}"
                    links.append(f"[{escape(ref.path)}:{ref.line_start}]({url})")
            label = "  解释输入范围（语义支持尚未独立核验）：" if claim.source_scope else "  证据："
            result.append(label + "、".join(links))
            if claim.source_scope and claim.source_scope.relationship_view == "resolution_counts":
                result.append("  汇总关系视图：按类型和真实解析状态计数；模型未读取本层逐条符号关系。完整关系见服务侧图与知识详情，此统计不证明模块依赖或完整调用路径。")
            elif claim.source_scope and claim.source_scope.relationship_view == "symbol_graph":
                result.append("  汇总关系视图：本层完整符号关系；候选与未解析目标仍保持原状态。")
        return result

    rows.extend(render_claims(card.claims))
    if card.links:
        rows.extend(["", "## 关联知识", ""])
        for cid in card.links:
            linked = all_cards[cid]
            # 文档在源码 SHA 之后才提交，导航使用相对路径；源码证据仍固定 SHA。
            target = quote(posixpath.relpath(card_path(linked), posixpath.dirname(card_path(card)) or "."), safe="/.")
            rows.append(f"- [{escape(linked.title)}]({target})")
    if card.unknowns:
        rows.extend(["", "## 限制与未知项", "", *["- " + escape(u) for u in card.unknowns]])
    if card.detail_claims or card.detail_unknowns:
        rows.extend(["", "<details>", "<summary>完整解释与限制</summary>", ""])
        if card.detail_claims:
            rows.extend(["### 详细解释", "", *render_claims(card.detail_claims)])
        if card.detail_unknowns:
            rows.extend(["", "### 完整未知项", "", *["- " + escape(u) for u in card.detail_unknowns]])
        rows.extend(["", "</details>"])
    return "\n".join(rows) + "\n"


def render_repository(cards: list[KnowledgeCard], manifest: BrainManifest, bundle: FactBundle, project_url: str) -> dict[str, str]:
    """仅生成白名单文件，Markdown 没有独立模型调用。"""
    output = {}
    for card in cards:
        md = card_path(card)
        json_path = "docs/project-brain/project.json" if card.kind == "project" else (md[:-3] if md.endswith(".md") else md) + ".json"
        if (md == json_path or md == "docs/project-brain/manifest.json" or json_path == "docs/project-brain/manifest.json"
                or md in output or json_path in output):
            raise ValueError("知识文档路径重复或占用了清单位置")
        output[md] = render_card(card, manifest, bundle, project_url, cards)
        output[json_path] = encoded(card.model_dump()) + "\n"
    return output
