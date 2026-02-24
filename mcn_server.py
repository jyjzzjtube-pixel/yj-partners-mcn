# -*- coding: utf-8 -*-
"""
YJ MCN 자동화 대시보드 — Flask 백엔드
=====================================
기존 affiliate_system 모듈을 REST API로 래핑.
SSE로 파이프라인 진행상황 실시간 스트리밍.

실행: python yj-partners-mcn/mcn_server.py
"""
import json
import os
import sys
import time
import uuid
import threading
from pathlib import Path
from queue import Queue
from datetime import datetime

from flask import Flask, request, jsonify, Response, send_file, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

# 프로젝트 루트 설정
PROJECT_DIR = Path(__file__).parent.parent  # franchise-db/
sys.path.insert(0, str(PROJECT_DIR))
load_dotenv(PROJECT_DIR / '.env', override=True)

from affiliate_system.config import (
    GEMINI_API_KEY, ANTHROPIC_API_KEY, PEXELS_API_KEY,
    PIXABAY_API_KEY, UNSPLASH_ACCESS_KEY, RENDER_OUTPUT_DIR, WORK_DIR,
)
from affiliate_system.models import Platform, PLATFORM_PRESETS

# ── 커맨드센터 AI 서비스 연동 ──
sys.path.insert(0, str(PROJECT_DIR))
from command_center.config import OPENAI_API_KEY, OLLAMA_BASE_URL, OLLAMA_MODEL, AI_PROVIDERS
from command_center.services.ai_service import AIService

ai_service = AIService()

# ── Flask 앱 설정 ──
app = Flask(__name__, static_folder=str(Path(__file__).parent))
CORS(app)

# ── Job 저장소 & 캠페인 히스토리 ──
jobs = {}  # job_id -> {status, step, progress, results, events, error}
campaign_history = []  # 캠페인 이력 (최근 50개)

# ── 캠페인 DB (SQLite) ──
import sqlite3
CAMPAIGN_DB = str(PROJECT_DIR / "mcn_campaigns.db")

def _init_campaign_db():
    """캠페인 히스토리 DB 초기화"""
    conn = sqlite3.connect(CAMPAIGN_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        topic TEXT, brand TEXT, platforms TEXT,
        ai_provider TEXT, cost_usd REAL DEFAULT 0,
        status TEXT DEFAULT 'pending',
        results TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.commit()
    conn.close()

_init_campaign_db()

# ── 브랜드 설정 ──
BRANDS = {
    "오레노카츠": {
        "name": "오레노카츠",
        "type": "Japanese Tonkatsu Franchise",
        "emoji": "🍱",
        "platforms": ["youtube", "naver_blog", "instagram"],
        "campaigns": 12,
    },
    "무사짬뽕": {
        "name": "무사짬뽕",
        "type": "Chinese Jjamppong Franchise",
        "emoji": "🍜",
        "platforms": ["youtube", "naver_blog", "instagram"],
        "campaigns": 8,
    },
    "브릿지원": {
        "name": "브릿지원",
        "type": "Franchise Consulting (BRIDGE ONE)",
        "emoji": "💼",
        "platforms": ["youtube", "naver_blog", "instagram", "coupang"],
        "campaigns": 15,
    },
}

PLATFORM_MAP = {
    "youtube": Platform.YOUTUBE,
    "instagram": Platform.INSTAGRAM,
    "naver_blog": Platform.NAVER_BLOG,
}


# ═══════════════════════════════════════════════════════════════
# 유틸리티
# ═══════════════════════════════════════════════════════════════

def _to_relative_path(abs_path) -> str:
    """절대 경로를 PROJECT_DIR 기준 상대 경로로 변환 (프론트엔드 파일 서빙용)."""
    if not abs_path:
        return ""
    p = Path(str(abs_path))
    try:
        return str(p.relative_to(PROJECT_DIR)).replace("\\", "/")
    except ValueError:
        # PROJECT_DIR 밖의 경로면 그대로 반환
        return str(p).replace("\\", "/")


def serialize_results(results: dict) -> dict:
    """파이프라인 결과를 JSON 직렬화 가능한 형태로 변환."""
    out = {"platforms": {}, "upload_results": {}}
    for p_name, p_data in results.get("platforms", {}).items():
        out["platforms"][p_name] = {
            "video": _to_relative_path(p_data.get("video")) or None,
            "thumbnail": _to_relative_path(p_data.get("thumbnail")) or None,
            "content": p_data.get("content", {}),
        }
    out["upload_results"] = results.get("upload_results", {})
    campaign = results.get("campaign")
    if campaign:
        out["campaign_id"] = campaign.id
        out["created_at"] = campaign.created_at.isoformat() if campaign.created_at else None
    return out


# ═══════════════════════════════════════════════════════════════
# WebPipeline — 단계별 SSE 이벤트 발생 래퍼
# ═══════════════════════════════════════════════════════════════

class WebPipeline:
    """ContentPipeline을 단계별로 실행하며 SSE 이벤트를 발생시킨다."""

    def __init__(self, events_queue: Queue):
        self._q = events_queue

    def _emit(self, step: int, name: str, status: str, detail: str = ""):
        self._q.put({
            "type": "step",
            "step": step,
            "name": name,
            "status": status,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
        })

    def run(self, topic: str, platforms: list, brand: str, persona: str,
            auto_upload: bool, drive_archive: bool = True) -> dict:
        from affiliate_system.pipeline import ContentPipeline

        pipeline = ContentPipeline()
        platform_enums = [PLATFORM_MAP[p] for p in platforms if p in PLATFORM_MAP]
        if not platform_enums:
            platform_enums = list(PLATFORM_MAP.values())

        results = {"platforms": {}, "upload_results": {}}

        # Step 1: 상품 정보
        self._emit(1, "product_info", "running", "상품 정보 수집 중...")
        try:
            product = pipeline._prepare_product(topic)
            self._emit(1, "product_info", "complete", f"상품: {product.title}")
        except Exception as e:
            self._emit(1, "product_info", "error", str(e))
            raise

        # Step 2: AI 콘텐츠 생성
        self._emit(2, "ai_content", "running", f"{len(platform_enums)}개 플랫폼 AI 콘텐츠 생성 중...")
        try:
            platform_contents = pipeline._generate_contents(product, platform_enums, persona, brand)
            detail_parts = []
            for p_name, content in platform_contents.items():
                narr = len(content.get("narration", []))
                tags = len(content.get("hashtags", []))
                detail_parts.append(f"{p_name}: 나레이션 {narr}장면, 태그 {tags}개")
            self._emit(2, "ai_content", "complete", " | ".join(detail_parts))
        except Exception as e:
            self._emit(2, "ai_content", "error", str(e))
            raise

        # Step 3: 미디어 수집
        self._emit(3, "media", "running", "무료 스톡 이미지 수집 중...")
        try:
            images = pipeline._collect_media(product)
            self._emit(3, "media", "complete", f"이미지 {len(images)}개 수집")
        except Exception as e:
            self._emit(3, "media", "error", str(e))
            images = []

        # Step 4: 썸네일 생성
        self._emit(4, "thumbnail", "running", "플랫폼별 썸네일 생성 중...")
        try:
            campaign_id = uuid.uuid4().hex[:8]
            thumbnails = pipeline._generate_thumbnails(
                platform_enums, platform_contents, images, brand, campaign_id,
            )
            self._emit(4, "thumbnail", "complete", f"{len(thumbnails)}개 썸네일 생성")
        except Exception as e:
            self._emit(4, "thumbnail", "error", str(e))
            thumbnails = {}

        # Step 5: 영상 렌더링
        self._emit(5, "video_render", "running", "영상 렌더링 중 (시간 소요)...")
        try:
            videos = pipeline._render_videos(
                platform_enums, platform_contents, images, brand, campaign_id,
            )
            rendered = [p for p, v in videos.items() if v]
            self._emit(5, "video_render", "complete", f"{len(rendered)}개 영상 렌더링 완료")
        except Exception as e:
            self._emit(5, "video_render", "error", str(e))
            videos = {}

        # 결과 조합 (절대 경로 → 상대 경로 변환)
        for p in platform_enums:
            p_name = p.value
            results["platforms"][p_name] = {
                "video": _to_relative_path(videos.get(p_name)) or None,
                "thumbnail": _to_relative_path(thumbnails.get(p_name)) or None,
                "content": platform_contents.get(p_name, {}),
            }

        # Step 6: 소셜 미디어 업로드
        if auto_upload:
            self._emit(6, "upload", "running", "3플랫폼 업로드 중...")
            try:
                from affiliate_system.models import Campaign, AIContent, CampaignStatus
                campaign_obj = Campaign(
                    id=campaign_id, product=product,
                    ai_content=AIContent(platform_contents=platform_contents),
                    status=CampaignStatus.UPLOADING,
                    target_platforms=platform_enums,
                    platform_videos=videos, platform_thumbnails=thumbnails,
                    created_at=datetime.now(),
                )
                upload_results = pipeline._upload_all(campaign_obj)
                results["upload_results"] = upload_results
                self._emit(6, "upload", "complete", "업로드 완료")
            except Exception as e:
                self._emit(6, "upload", "error", str(e))
        else:
            self._emit(6, "upload", "skipped", "업로드 건너뜀 (수동 모드)")

        # Step 7: Google Drive 자동 아카이빙
        if drive_archive:
            self._emit(7, "drive_archive", "running", "Google Drive 폴더 생성 및 업로드 중...")
            try:
                from affiliate_system.drive_manager import DriveArchiver
                from affiliate_system.models import Campaign, AIContent, CampaignStatus

                # Campaign 객체 생성
                campaign_obj = Campaign(
                    id=campaign_id, product=product,
                    ai_content=AIContent(platform_contents=platform_contents),
                    status=CampaignStatus.COMPLETE,
                    target_platforms=platform_enums,
                    platform_videos=videos, platform_thumbnails=thumbnails,
                    created_at=datetime.now(),
                )

                # 업로드할 파일 분류
                drive_files = {"images": [], "renders": [], "audio": [], "logs": []}

                # 렌더링 결과 (영상 + 썸네일)
                for p_name, v_path in videos.items():
                    if v_path and Path(str(v_path)).exists():
                        drive_files["renders"].append(str(v_path))
                for p_name, t_path in thumbnails.items():
                    if t_path and Path(str(t_path)).exists():
                        drive_files["renders"].append(str(t_path))

                # 수집된 이미지
                media_dir = WORK_DIR / "media_downloads"
                if media_dir.exists():
                    for img in sorted(media_dir.glob("*.*"))[-20:]:  # 최근 20개
                        if img.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                            drive_files["images"].append(str(img))

                # TTS 오디오 (최근 생성된 tts_ 폴더)
                for tts_dir in sorted(WORK_DIR.glob("tts_*"), reverse=True)[:1]:
                    if tts_dir.is_dir():
                        for audio_f in tts_dir.glob("*.mp3"):
                            drive_files["audio"].append(str(audio_f))

                total_files = sum(len(v) for v in drive_files.values())
                self._emit(7, "drive_archive", "running",
                           f"{total_files}개 파일 Drive 업로드 중...")

                # DriveArchiver로 업로드
                archiver = DriveArchiver()
                if archiver.authenticate():
                    def _progress(cur, tot, fname):
                        self._emit(7, "drive_archive", "running",
                                   f"Drive 업로드 {cur}/{tot}: {fname}")

                    archive_result = archiver.archive_campaign(
                        campaign_obj, drive_files, progress_callback=_progress
                    )

                    if archive_result["ok"]:
                        folder_url = archive_result.get("folder_url", "")
                        uploaded = archive_result["files_uploaded"]
                        self._emit(7, "drive_archive", "complete",
                                   f"Drive 아카이빙 완료: {uploaded}개 파일 업로드")
                        results["drive_url"] = folder_url
                        results["drive_files_uploaded"] = uploaded
                    else:
                        errors = archive_result.get("errors", [])
                        self._emit(7, "drive_archive", "error",
                                   f"일부 실패: {', '.join(errors[:3])}")
                else:
                    self._emit(7, "drive_archive", "error",
                               "Drive 인증 실패 — OAuth 토큰을 확인하세요")
            except Exception as e:
                self._emit(7, "drive_archive", "error", str(e))
        else:
            self._emit(7, "drive_archive", "skipped", "Drive 아카이빙 건너뜀")

        results["campaign_id"] = campaign_id
        return results


# ═══════════════════════════════════════════════════════════════
# API 엔드포인트
# ═══════════════════════════════════════════════════════════════

# ── 메인 페이지 ──
@app.route('/')
def index():
    return send_file(str(Path(__file__).parent / 'index.html'))


# ── 건강 체크 (AI 8개 + 미디어 + OpenClaw) ──
@app.route('/api/health')
def health():
    # AI 프로바이더 상태
    providers = ai_service.list_providers()
    services = {}
    for p in providers:
        services[p["name"]] = p["available"]
    # 미디어 API
    services["pexels"] = bool(PEXELS_API_KEY)
    services["pixabay"] = bool(PIXABAY_API_KEY)
    services["unsplash"] = bool(UNSPLASH_ACCESS_KEY)
    # OpenClaw 게이트웨이
    try:
        import requests as req
        oc = req.get("http://127.0.0.1:18792/__openclaw__/health", timeout=2)
        services["openclaw"] = oc.status_code == 200
    except Exception:
        services["openclaw"] = False

    return jsonify({
        "status": "online",
        "services": services,
        "active_jobs": sum(1 for j in jobs.values() if j["status"] == "running"),
        "ai_providers": providers,
        "timestamp": datetime.now().isoformat(),
    })


# ── 브랜드 목록 ──
@app.route('/api/brands')
def get_brands():
    return jsonify(BRANDS)


# ── 캠페인 시작 ──
@app.route('/api/campaign/start', methods=['POST'])
def start_campaign():
    data = request.json or {}
    topic = data.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "topic 필수"}), 400

    brand = data.get("brand", "")
    platforms = data.get("platforms", ["youtube", "instagram", "naver_blog"])
    persona = data.get("persona", "")
    auto_upload = data.get("auto_upload", False)
    drive_archive = data.get("drive_archive", True)  # 기본 ON

    job_id = uuid.uuid4().hex[:12]
    events_queue = Queue()

    jobs[job_id] = {
        "status": "pending",
        "step": 0,
        "topic": topic,
        "brand": brand,
        "platforms": platforms,
        "results": None,
        "error": None,
        "events": events_queue,
        "created_at": datetime.now().isoformat(),
    }

    # 캠페인 히스토리 저장 (시작)
    _save_campaign(job_id, topic, brand, platforms, "running")

    def worker():
        job = jobs[job_id]
        job["status"] = "running"
        try:
            wp = WebPipeline(events_queue)
            results = wp.run(topic, platforms, brand, persona, auto_upload, drive_archive)
            job["results"] = results
            job["status"] = "complete"
            events_queue.put({"type": "complete", "results": results})
            # 캠페인 히스토리 업데이트 (완료)
            _save_campaign(job_id, topic, brand, platforms, "complete",
                           results=_safe_serialize(results))
        except Exception as e:
            job["error"] = str(e)
            job["status"] = "error"
            events_queue.put({"type": "error", "error": str(e)})
            _save_campaign(job_id, topic, brand, platforms, "error")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


# ── SSE 진행상황 스트리밍 ──
@app.route('/api/campaign/stream/<job_id>')
def stream_campaign(job_id):
    def generate():
        job = jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Job not found'})}\n\n"
            return

        q = job["events"]
        while job["status"] in ("pending", "running"):
            while not q.empty():
                event = q.get_nowait()
                # 결과를 직렬화 가능하게 변환
                if event.get("type") == "complete" and event.get("results"):
                    event["results"] = _safe_serialize(event["results"])
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            time.sleep(0.3)

        # 잔여 이벤트 flush
        while not q.empty():
            event = q.get_nowait()
            if event.get("type") == "complete" and event.get("results"):
                event["results"] = _safe_serialize(event["results"])
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        # 최종 상태
        if job["status"] == "complete" and job["results"]:
            yield f"data: {json.dumps({'type': 'done', 'results': _safe_serialize(job['results'])}, ensure_ascii=False, default=str)}\n\n"
        elif job["status"] == "error":
            yield f"data: {json.dumps({'type': 'error', 'error': job['error']})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


def _safe_serialize(obj):
    """결과를 JSON 직렬화 가능하게 변환."""
    if isinstance(obj, dict):
        return {k: _safe_serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_serialize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, '__dict__') and not isinstance(obj, type):
        return str(obj)
    return obj


# ── AI 콘텐츠만 생성 (영상 없이) ──
@app.route('/api/content/generate', methods=['POST'])
def generate_content():
    data = request.json or {}
    topic = data.get("topic", "").strip()
    if not topic:
        return jsonify({"error": "topic 필수"}), 400

    brand = data.get("brand", "")
    persona = data.get("persona", "")
    platforms = data.get("platforms", ["youtube", "instagram", "naver_blog"])

    try:
        from affiliate_system.pipeline import ContentPipeline
        pipeline = ContentPipeline()
        product = pipeline._prepare_product(topic)

        platform_enums = [PLATFORM_MAP[p] for p in platforms if p in PLATFORM_MAP]
        contents = pipeline._generate_contents(product, platform_enums, persona, brand)

        return jsonify({
            "product": {"title": product.title, "description": product.description},
            "contents": contents,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 무료 미디어 검색 ──
@app.route('/api/media/search', methods=['POST'])
def search_media():
    data = request.json or {}
    query = data.get("query", "").strip()
    media_type = data.get("type", "image")  # image or video

    if not query:
        return jsonify({"error": "query 필수"}), 400

    try:
        from affiliate_system.media_collector import MediaCollector
        mc = MediaCollector()

        if media_type == "video":
            results = mc.search_videos(query, count=6)
        else:
            results = mc.search_images(query, count=12)

        return jsonify({"results": results, "query": query, "type": media_type})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 소셜 미디어 다운로드 (TikTok, YouTube 등) ──
@app.route('/api/media/social', methods=['POST'])
def download_social():
    data = request.json or {}
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "url 필수"}), 400

    try:
        from affiliate_system.media_collector import MediaCollector
        mc = MediaCollector()
        result = mc.download_from_social(url)
        return jsonify({"result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── AI 이미지 생성 (Gemini) ──
@app.route('/api/media/ai-generate', methods=['POST'])
def ai_generate():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    media_type = data.get("type", "image")

    if not prompt:
        return jsonify({"error": "prompt 필수"}), 400

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)

        if media_type == "image":
            # Gemini 이미지 생성
            model = genai.GenerativeModel('gemini-2.0-flash-exp')
            response = model.generate_content(prompt)
            # 텍스트 응답에서 이미지 프롬프트 반환
            return jsonify({
                "type": "image",
                "prompt_used": prompt,
                "response": response.text if response.text else "생성 완료",
            })
        else:
            return jsonify({"error": "video AI 생성은 준비 중"}), 501
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 비용 현황 (커맨드센터 연동) ──
@app.route('/api/cost')
def get_cost():
    try:
        from api_cost_tracker import CostTracker
        tracker = CostTracker()
        summary = tracker.get_summary()
    except Exception:
        summary = {"total_usd": 0}

    # AI 프로바이더별 비용 정보
    summary["providers"] = {}
    for name, info in AI_PROVIDERS.items():
        summary["providers"][name] = {
            "model": info["model"],
            "cost_tier": info["cost"],
        }
    return jsonify(summary)


# ── AI 프로바이더 목록 ──
@app.route('/api/ai/providers')
def ai_providers():
    return jsonify(ai_service.list_providers())


# ── AI 직접 호출 (테스트/단독 사용) ──
@app.route('/api/ai/ask', methods=['POST'])
def ai_ask():
    data = request.json or {}
    prompt = data.get("prompt", "").strip()
    provider = data.get("provider")  # None이면 자동 폴백
    if not prompt:
        return jsonify({"error": "prompt 필수"}), 400

    try:
        resp = ai_service.ask(prompt, provider=provider)
        return jsonify({
            "text": resp.text,
            "provider": resp.provider,
            "model": resp.model,
            "tokens": {"input": resp.input_tokens, "output": resp.output_tokens},
            "cost_usd": resp.cost_usd,
            "elapsed_ms": resp.elapsed_ms,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 캠페인 히스토리 ──
@app.route('/api/campaigns')
def list_campaigns():
    """최근 캠페인 이력 조회"""
    limit = request.args.get("limit", 20, type=int)
    conn = sqlite3.connect(CAMPAIGN_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM campaigns ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/campaigns/<campaign_id>')
def get_campaign(campaign_id):
    """특정 캠페인 상세 조회"""
    conn = sqlite3.connect(CAMPAIGN_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "캠페인 없음"}), 404
    result = dict(row)
    # 결과 JSON 파싱
    if result.get("results"):
        try:
            result["results"] = json.loads(result["results"])
        except Exception:
            pass
    return jsonify(result)


def _save_campaign(campaign_id, topic, brand, platforms, status, results=None, cost=0.0):
    """캠페인 이력 DB 저장"""
    conn = sqlite3.connect(CAMPAIGN_DB)
    conn.execute("""INSERT OR REPLACE INTO campaigns
        (id, topic, brand, platforms, status, results, cost_usd, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (campaign_id, topic, brand, json.dumps(platforms),
         status, json.dumps(results) if results else None,
         cost, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


# ── 파일 다운로드/미리보기 (렌더링된 영상/이미지) ──
@app.route('/api/file/<path:filepath>')
def serve_file(filepath):
    full_path = PROJECT_DIR / filepath
    if full_path.exists() and full_path.is_file():
        # MIME 타입 자동 감지 + 비디오/이미지는 inline 표시
        suffix = full_path.suffix.lower()
        mime_map = {
            '.mp4': 'video/mp4', '.webm': 'video/webm', '.avi': 'video/x-msvideo',
            '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
            '.gif': 'image/gif', '.webp': 'image/webp',
        }
        mimetype = mime_map.get(suffix)
        return send_file(str(full_path), mimetype=mimetype)
    return jsonify({"error": "파일 없음"}), 404


# ── 렌더링 출력 폴더 직접 목록 (디버깅용) ──
@app.route('/api/renders')
def list_renders():
    renders_dir = PROJECT_DIR / "affiliate_system" / "renders"
    if not renders_dir.exists():
        return jsonify({"files": []})
    files = []
    for f in sorted(renders_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            files.append({
                "name": f.name,
                "size_mb": round(f.stat().st_size / (1024*1024), 2),
                "url": f"/api/file/affiliate_system/renders/{f.name}",
                "modified": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return jsonify({"files": files[:50]})


# ── Google Drive 상태 ──
@app.route('/api/drive/status')
def drive_status():
    """Google Drive 인증 상태 및 저장용량 확인"""
    try:
        from affiliate_system.drive_manager import DriveArchiver
        archiver = DriveArchiver()
        token_path = archiver.TOKEN_PATH
        token_exists = token_path.exists()

        if token_exists and archiver.authenticate():
            usage = archiver.get_storage_usage()
            return jsonify({
                "authenticated": True,
                "token_path": str(token_path),
                "storage": usage,
            })
        else:
            return jsonify({
                "authenticated": False,
                "token_path": str(token_path),
                "token_exists": token_exists,
                "error": "인증 필요" if not token_exists else "토큰 만료",
            })
    except Exception as e:
        return jsonify({"authenticated": False, "error": str(e)})


@app.route('/api/drive/campaigns')
def drive_campaigns():
    """Google Drive에 아카이빙된 캠페인 목록"""
    try:
        from affiliate_system.drive_manager import DriveArchiver
        archiver = DriveArchiver()
        if archiver.authenticate():
            campaigns = archiver.list_campaigns()
            return jsonify({"campaigns": campaigns})
        return jsonify({"error": "Drive 인증 실패"}), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── 캠페인 상태 조회 ──
@app.route('/api/campaign/status/<job_id>')
def campaign_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    resp = {
        "job_id": job_id,
        "status": job["status"],
        "topic": job["topic"],
        "brand": job["brand"],
        "platforms": job["platforms"],
        "created_at": job["created_at"],
        "error": job.get("error"),
    }
    if job["status"] == "complete" and job["results"]:
        resp["results"] = _safe_serialize(job["results"])
    return jsonify(resp)


# ═══════════════════════════════════════════════════════════════
# V2 — 대화형 쿠팡 수익 극대화 파이프라인 API
# ═══════════════════════════════════════════════════════════════

# V2 Job 저장소 (interactive 상태머신)
v2_jobs = {}  # job_id -> {state, coupang_link, product, draft, events, ...}


class V2PipelineState:
    """V2 대화형 파이프라인 상태 enum."""
    IDLE = "idle"
    AWAITING_LINK = "awaiting_link"
    ANALYZING = "analyzing"
    AWAITING_CONFIRM = "awaiting_confirm"
    EXECUTING = "executing"
    COMPLETE = "complete"
    ERROR = "error"


# V2 10단계 정의
V2_STEPS = [
    {"step": 1,  "name": "prep_report",    "label": "준비 리포트",           "module": "pipeline"},
    {"step": 2,  "name": "link_analysis",   "label": "쿠팡 링크 분석",       "module": "coupang_scraper"},
    {"step": 3,  "name": "ai_content",      "label": "AI 콘텐츠 생성",       "module": "ai_generator V2"},
    {"step": 4,  "name": "media_crawl",     "label": "미디어 크롤링",         "module": "OmniMediaCollector"},
    {"step": 5,  "name": "blog_compose",    "label": "블로그 HTML 조립",      "module": "blog_html_generator"},
    {"step": 6,  "name": "video_launder",   "label": "4단계 영상 세탁",       "module": "video_launderer (FFmpeg GPU)"},
    {"step": 7,  "name": "shorts_render",   "label": "숏폼 렌더링",          "module": "ShortsRenderer + TTS + Whisper"},
    {"step": 8,  "name": "thumbnail",       "label": "썸네일 생성",          "module": "thumbnail_generator"},
    {"step": 9,  "name": "upload_ready",    "label": "업로드 준비",          "module": "auto_uploader V2"},
    {"step": 10, "name": "drive_archive",   "label": "Drive 아카이빙",       "module": "drive_manager"},
]


@app.route('/api/v2/steps')
def v2_steps():
    """V2 10단계 파이프라인 정의 반환."""
    return jsonify(V2_STEPS)


@app.route('/api/v2/campaign/start', methods=['POST'])
def v2_start_campaign():
    """V2 대화형 캠페인 시작 — "쿠팡 링크를 보내주세요" 상태로 진입."""
    job_id = uuid.uuid4().hex[:12]
    events_queue = Queue()

    v2_jobs[job_id] = {
        "state": V2PipelineState.AWAITING_LINK,
        "coupang_link": None,
        "product_info": None,
        "draft": None,
        "blog_html": None,
        "shorts_script": None,
        "results": {},
        "error": None,
        "events": events_queue,
        "created_at": datetime.now().isoformat(),
        "platforms": ["naver_blog", "youtube", "instagram"],
    }

    events_queue.put({
        "type": "state_change",
        "state": V2PipelineState.AWAITING_LINK,
        "message": "🔗 쿠팡 파트너스 링크를 입력해 주세요!",
        "timestamp": datetime.now().isoformat(),
    })

    return jsonify({
        "job_id": job_id,
        "state": V2PipelineState.AWAITING_LINK,
        "message": "쿠팡 파트너스 링크를 입력해 주세요",
    })


@app.route('/api/v2/campaign/<job_id>/link', methods=['POST'])
def v2_submit_link(job_id):
    """V2 쿠팡 링크 제출 → 상품 분석 시작."""
    job = v2_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] != V2PipelineState.AWAITING_LINK:
        return jsonify({"error": f"현재 상태: {job['state']}, 링크 입력 불가"}), 400

    data = request.json or {}
    coupang_link = data.get("coupang_link", "").strip()       # 상품정보 URL (스크래핑용)
    affiliate_link = data.get("affiliate_link", "").strip()   # 단축 URL (수익 링크)
    banner_tag = data.get("banner_tag", "").strip()           # 쿠팡 배너 코드 (<a><img> 또는 iframe)
    product_name = data.get("product_name", "").strip()

    # 디버그 로그 파일 기록
    from pathlib import Path as _Path
    _dbg = _Path(__file__).parent.parent / "affiliate_system" / "workspace" / "v2_debug.log"
    with open(_dbg, "w", encoding="utf-8") as _f:
        _f.write(f"[SUBMIT] coupang_link={coupang_link[:80]}\n")
        _f.write(f"[SUBMIT] affiliate_link={affiliate_link}\n")
        _f.write(f"[SUBMIT] banner_tag_len={len(banner_tag)}\n")
        _f.write(f"[SUBMIT] banner_tag={banner_tag[:200]}\n")
        _f.write(f"[SUBMIT] product_name_input={product_name}\n")

    # 배너코드 alt 속성에서 상품명 자동 추출 (사용자가 상품명 미입력 시)
    if not product_name and banner_tag:
        import re as _re
        _alt_match = _re.search(r'alt=["\']([^"\']+)["\']', banner_tag)
        if _alt_match:
            product_name = _alt_match.group(1).strip()
            with open(_dbg, "a", encoding="utf-8") as _f:
                _f.write(f"[ALT_EXTRACT] product_name={product_name}\n")
        else:
            with open(_dbg, "a", encoding="utf-8") as _f:
                _f.write(f"[ALT_EXTRACT] NO MATCH in banner_tag\n")
    elif product_name:
        with open(_dbg, "a", encoding="utf-8") as _f:
            _f.write(f"[ALT_EXTRACT] SKIPPED - product_name already set: {product_name}\n")
    else:
        with open(_dbg, "a", encoding="utf-8") as _f:
            _f.write(f"[ALT_EXTRACT] SKIPPED - no banner_tag\n")

    if not coupang_link:
        return jsonify({"error": "상품정보 링크 필수"}), 400
    if not affiliate_link:
        return jsonify({"error": "단축 URL 필수"}), 400

    job["coupang_link"] = coupang_link
    job["affiliate_link"] = affiliate_link
    job["banner_tag"] = banner_tag
    job["product_name"] = product_name
    job["state"] = V2PipelineState.ANALYZING
    job["events"].put({
        "type": "state_change",
        "state": V2PipelineState.ANALYZING,
        "message": "🔍 상품 분석 중...",
        "timestamp": datetime.now().isoformat(),
    })

    # 비동기 분석
    def analyze():
        try:
            # Step 1: 준비
            job["events"].put({
                "type": "v2_step", "step": 1, "name": "prep_report",
                "status": "complete", "detail": "V2 파이프라인 초기화 완료",
                "timestamp": datetime.now().isoformat(),
            })

            # Step 2: 링크 분석
            job["events"].put({
                "type": "v2_step", "step": 2, "name": "link_analysis",
                "status": "running", "detail": "쿠팡 링크 스크래핑 중...",
                "timestamp": datetime.now().isoformat(),
            })

            from affiliate_system.pipeline import ContentPipeline
            from affiliate_system.models import Product
            pipeline = ContentPipeline()
            product = pipeline._prepare_product(coupang_link)

            # 디버그 로그 — 스크래핑 결과 + 폴백 판단
            _dbg = _Path(__file__).parent.parent / "affiliate_system" / "workspace" / "v2_debug.log"
            with open(_dbg, "a", encoding="utf-8") as _f:
                _f.write(f"[SCRAPE] product.title={product.title}\n")
                _f.write(f"[SCRAPE] product.description={str(product.description)[:100]}\n")
                _f.write(f"[SCRAPE] product_name_var={product_name}\n")

            # 쿠팡 스크래핑 실패 시 배너코드 alt → 사용자 입력 상품명 순으로 폴백
            _bad_titles = ("쿠팡 상품", "인기상품", "", None)
            if not product.title or product.title in _bad_titles:
                # 1차 폴백: 사용자 입력 또는 배너코드 alt에서 추출된 상품명
                if product_name:
                    with open(_dbg, "a", encoding="utf-8") as _f:
                        _f.write(f"[FALLBACK] Using product_name: {product_name}\n")
                    product = Product(
                        title=product_name,
                        description=f"{product_name} - 쿠팡 최저가 상품",
                        url=coupang_link,
                        affiliate_link=affiliate_link,
                        scraped_at=product.scraped_at,
                    )
                else:
                    with open(_dbg, "a", encoding="utf-8") as _f:
                        _f.write(f"[FALLBACK] WARNING: No product_name, using default\n")
            elif product_name and product.title != product_name:
                # 스크래핑 성공했지만 배너 alt와 다른 경우 → 배너 alt 우선 (더 정확)
                print(f"[V2] 배너코드 상품명으로 교체: {product.title} → {product_name}")
                product = Product(
                    title=product_name,
                    description=product.description or f"{product_name} - 쿠팡 최저가",
                    url=coupang_link,
                    affiliate_link=affiliate_link,
                    scraped_at=product.scraped_at,
                    price=getattr(product, 'price', ''),
                    images=getattr(product, 'images', []),
                )

            # 항상 수익 링크를 파트너스 링크로 설정
            product.affiliate_link = affiliate_link

            product_info = {
                "title": product.title,
                "description": product.description or "",
                "price": product.price or "",
                "image_urls": product.image_urls[:3] if product.image_urls else [],
                "affiliate_link": affiliate_link,  # 수익 링크 (link.coupang.com/a/...)
                "product_url": coupang_link,        # 상품정보 링크 (coupang.com/vp/products/...)
            }
            job["product_info"] = product_info

            job["events"].put({
                "type": "v2_step", "step": 2, "name": "link_analysis",
                "status": "complete",
                "detail": f"상품: {product.title}",
                "timestamp": datetime.now().isoformat(),
            })

            # Step 3: AI 콘텐츠 초안 생성
            job["events"].put({
                "type": "v2_step", "step": 3, "name": "ai_content",
                "status": "running", "detail": "블로그 + 숏폼 대본 AI 생성 중 (Gemini 무료)...",
                "timestamp": datetime.now().isoformat(),
            })

            try:
                from affiliate_system.ai_generator import AIGenerator
                generator = AIGenerator()
                # 디버그: AI 생성 직전 최종 product.title 확인
                with open(_dbg, "a", encoding="utf-8") as _f:
                    _f.write(f"[AI_GEN] FINAL product.title={product.title}\n")
                    _f.write(f"[AI_GEN] FINAL product.description={str(product.description)[:100]}\n")

                # V2 블로그 콘텐츠 — 수익 링크로 생성
                blog_content = generator.generate_blog_content_v2(product, affiliate_link)
                job["draft"] = {
                    "blog": blog_content,
                    "product": product_info,
                }

                # V2 숏폼 후킹 대본
                try:
                    shorts_scenes = generator.generate_shorts_hooking_script(
                        product, persona="", coupang_link=affiliate_link, dm_keyword="링크"
                    )
                    # list[dict] → {"scenes": [...]} 형태로 감싸기 (Step 7 호환)
                    if isinstance(shorts_scenes, list):
                        shorts_script = {"scenes": shorts_scenes}
                    elif isinstance(shorts_scenes, dict) and "scenes" in shorts_scenes:
                        shorts_script = shorts_scenes
                    else:
                        shorts_script = {"scenes": []}
                    job["shorts_script"] = shorts_script
                    job["draft"]["shorts"] = shorts_script
                    print(f"[V2] 숏폼 대본: {len(shorts_script.get('scenes', []))}장면 생성 완료")
                except Exception as se:
                    import traceback
                    print(f"[V2] 숏폼 대본 생성 실패: {se}")
                    traceback.print_exc()
                    job["draft"]["shorts"] = {"error": str(se)}

                job["events"].put({
                    "type": "v2_step", "step": 3, "name": "ai_content",
                    "status": "complete",
                    "detail": f"블로그 {len(blog_content.get('body_sections', []))}섹션 + 숏폼 대본 생성 완료",
                    "timestamp": datetime.now().isoformat(),
                })

            except Exception as ai_err:
                job["events"].put({
                    "type": "v2_step", "step": 3, "name": "ai_content",
                    "status": "error", "detail": str(ai_err),
                    "timestamp": datetime.now().isoformat(),
                })

            # 확인 대기 상태로 전환
            job["state"] = V2PipelineState.AWAITING_CONFIRM
            job["events"].put({
                "type": "state_change",
                "state": V2PipelineState.AWAITING_CONFIRM,
                "message": "✅ 분석 완료! 초안을 확인하고 실행 버튼을 눌러주세요.",
                "draft": job["draft"],
                "timestamp": datetime.now().isoformat(),
            })

        except Exception as e:
            job["state"] = V2PipelineState.ERROR
            job["error"] = str(e)
            job["events"].put({
                "type": "error", "error": str(e),
                "timestamp": datetime.now().isoformat(),
            })

    thread = threading.Thread(target=analyze, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "state": V2PipelineState.ANALYZING})


@app.route('/api/v2/campaign/<job_id>/confirm', methods=['POST'])
def v2_confirm_execute(job_id):
    """V2 실행 확인 → 나머지 7단계 (미디어 크롤링 ~ Drive 아카이빙) 실행."""
    job = v2_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] != V2PipelineState.AWAITING_CONFIRM:
        return jsonify({"error": f"현재 상태: {job['state']}, 실행 확인 불가"}), 400

    # 플랫폼별 업로드 토글 저장
    confirm_data = request.json or {}
    job["upload_youtube"] = confirm_data.get("upload_youtube", False)
    job["upload_instagram"] = confirm_data.get("upload_instagram", False)
    job["upload_naver"] = confirm_data.get("upload_naver", False)

    job["state"] = V2PipelineState.EXECUTING
    job["events"].put({
        "type": "state_change",
        "state": V2PipelineState.EXECUTING,
        "message": "🚀 실행 시작! 10단계 파이프라인 진행 중...",
        "timestamp": datetime.now().isoformat(),
    })

    def execute():
        try:
            coupang_link = job["coupang_link"]
            affiliate_link = job.get("affiliate_link", coupang_link)  # 수익 링크
            banner_tag = job.get("banner_tag", "")  # 쿠팡 배너 코드
            draft = job.get("draft", {})
            blog_content = draft.get("blog", {})
            product_info = job.get("product_info", {})
            product_title = product_info.get("title", "상품")

            # Step 4: 스마트 미디어 크롤링 + AI 이미지 생성
            job["events"].put({
                "type": "v2_step", "step": 4, "name": "media_crawl",
                "status": "running", "detail": "Gemini 키워드 분석 + 미디어 수집 + AI 이미지 생성...",
                "timestamp": datetime.now().isoformat(),
            })
            blog_images = []
            video_sources = []
            ai_images = []
            try:
                from affiliate_system.media_collector import OmniMediaCollector, MediaCollector
                from affiliate_system.ai_generator import AIGenerator
                omni = OmniMediaCollector()
                gen = AIGenerator()

                # ── Gemini SmartMediaMatcher: 주제 분석 → 최적 키워드 생성 ──
                product_features = product_info.get("features", "")
                if isinstance(product_features, list):
                    product_features = ", ".join(product_features)
                category = product_info.get("category", "")
                smart_keywords = gen.generate_smart_media_keywords(
                    product_name=product_title,
                    category=category,
                    product_features=product_features,
                )
                job["smart_keywords"] = smart_keywords
                job["category"] = smart_keywords.get("category_detected", category)
                job["product_name"] = product_title

                # 스마트 키워드로 이미지 검색
                image_kw_en = smart_keywords.get("image_keywords_en", [product_title])
                image_kw_ko = smart_keywords.get("image_keywords_ko", [product_title])
                all_image_kw = image_kw_en + image_kw_ko
                product_image_urls = product_info.get("image_urls", [])

                blog_images = omni.collect_blog_images(
                    product_title=product_title,
                    image_keywords=all_image_kw[:7],
                    product_image_urls=product_image_urls,
                    count=5,
                )

                # 스마트 키워드로 비디오 검색
                video_kw_en = smart_keywords.get("video_keywords_en", [])
                search_en = video_kw_en[0] if video_kw_en else gen.translate_for_search(product_title)
                try:
                    video_sources = omni.collect_video_sources(
                        product_title=product_title,
                        search_keyword_en=search_en,
                        count=6,
                    )
                except Exception:
                    video_sources = []

                # ── Gemini Imagen 4.0: AI 이미지 생성 (부족분 보충 + 고퀄 CTA) ──
                ai_prompts = smart_keywords.get("ai_image_prompts", [])
                if ai_prompts:
                    try:
                        from affiliate_system.config import V2_BLOG_DIR
                        ai_images = gen.generate_ai_images(
                            prompts=ai_prompts[:3],
                            output_dir=str(V2_BLOG_DIR / "ai_generated"),
                            count_per_prompt=1,
                            aspect_ratio="9:16",
                        )
                        # AI 이미지를 블로그 이미지 풀에 추가
                        blog_images.extend(ai_images)
                    except Exception as ai_err:
                        print(f"[V2] AI 이미지 생성 스킵: {ai_err}")

                job["events"].put({
                    "type": "v2_step", "step": 4, "name": "media_crawl",
                    "status": "complete",
                    "detail": (
                        f"크롤링 이미지 {len(blog_images)-len(ai_images)}장 + "
                        f"AI 이미지 {len(ai_images)}장 + "
                        f"영상 {len(video_sources)}개 수집"
                    ),
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as me:
                import traceback
                print(f"[V2] Step 4 미디어 크롤링 에러: {me}")
                print(traceback.format_exc())
                job["events"].put({
                    "type": "v2_step", "step": 4, "name": "media_crawl",
                    "status": "error", "detail": str(me),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 5: 블로그 HTML 조립
            job["events"].put({
                "type": "v2_step", "step": 5, "name": "blog_compose",
                "status": "running", "detail": "이미지-텍스트 교차 배치 HTML 생성 중...",
                "timestamp": datetime.now().isoformat(),
            })
            blog_html = ""
            try:
                from affiliate_system.blog_html_generator import NaverBlogHTMLGenerator
                html_gen = NaverBlogHTMLGenerator()
                blog_html = html_gen.generate_blog_html(
                    title=blog_content.get("title", product_info.get("title", "")),
                    intro=blog_content.get("intro", ""),
                    body_sections=blog_content.get("body_sections", []),
                    image_paths=[p for p in blog_images if p],  # 빈 경로 필터링
                    coupang_link=affiliate_link,  # 수익 링크 사용!
                    hashtags=blog_content.get("hashtags", []),
                    banner_tag=banner_tag,  # 쿠팡 배너 코드
                )
                job["blog_html"] = blog_html
                job["events"].put({
                    "type": "v2_step", "step": 5, "name": "blog_compose",
                    "status": "complete",
                    "detail": f"HTML {len(blog_html)}자 생성 (이미지 {len(blog_images)}장 교차 배치)",
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as he:
                job["events"].put({
                    "type": "v2_step", "step": 5, "name": "blog_compose",
                    "status": "error", "detail": str(he),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 6: 영상 세탁
            job["events"].put({
                "type": "v2_step", "step": 6, "name": "video_launder",
                "status": "running", "detail": "4단계 FFmpeg GPU 세탁 중...",
                "timestamp": datetime.now().isoformat(),
            })
            laundered_videos = []
            try:
                if video_sources:
                    from affiliate_system.video_launderer import VideoLaunderer
                    launderer = VideoLaunderer()
                    video_paths = [v["path"] for v in video_sources if v.get("path")]
                    laundered_videos = launderer.batch_launder(video_paths)
                    job["events"].put({
                        "type": "v2_step", "step": 6, "name": "video_launder",
                        "status": "complete",
                        "detail": f"{len(laundered_videos)}개 영상 세탁 완료",
                        "timestamp": datetime.now().isoformat(),
                    })
                else:
                    # 비디오 소스 없음 → 블로그 이미지를 영상 클립으로 변환 (Ken Burns)
                    if blog_images:
                        import subprocess
                        from affiliate_system.config import V2_SHORTS_DIR, FFMPEG_CRF
                        _img_vid_dir = _Path(V2_SHORTS_DIR) / "img_clips"
                        _img_vid_dir.mkdir(parents=True, exist_ok=True)

                        for img_i, img_path in enumerate(blog_images[:6]):
                            try:
                                out_clip = str(_img_vid_dir / f"img_clip_{img_i}_{job_id[:8]}.mp4")
                                # FFmpeg: 이미지 → 8초 영상 (zoompan Ken Burns 효과)
                                subprocess.run([
                                    "ffmpeg", "-y",
                                    "-loop", "1", "-i", str(img_path),
                                    "-vf", (
                                        "scale=1080:1920:force_original_aspect_ratio=increase,"
                                        "crop=1080:1920,"
                                        "zoompan=z='min(zoom+0.0015,1.3)':d=240:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s=1080x1920:fps=30"
                                    ),
                                    "-t", "8",
                                    "-c:v", "libx264",
                                    "-crf", FFMPEG_CRF,
                                    "-preset", "medium",
                                    "-pix_fmt", "yuv420p",
                                    "-an",  # 오디오 없음
                                    out_clip,
                                ], capture_output=True, timeout=60)

                                if os.path.exists(out_clip) and os.path.getsize(out_clip) > 10000:
                                    laundered_videos.append(out_clip)
                            except Exception as _img_err:
                                print(f"[V2] 이미지→영상 변환 실패 [{img_i}]: {_img_err}")

                        print(f"[V2] 이미지→영상 폴백: {len(laundered_videos)}개 생성")

                    job["events"].put({
                        "type": "v2_step", "step": 6, "name": "video_launder",
                        "status": "complete",
                        "detail": f"이미지→영상 폴백: {len(laundered_videos)}개 클립 생성" if laundered_videos else "영상/이미지 없음",
                        "timestamp": datetime.now().isoformat(),
                    })
            except Exception as le:
                job["events"].put({
                    "type": "v2_step", "step": 6, "name": "video_launder",
                    "status": "error", "detail": str(le),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 7: 숏폼 렌더링
            job["events"].put({
                "type": "v2_step", "step": 7, "name": "shorts_render",
                "status": "running", "detail": "TTS + 자막 싱크 + 숏폼 조립 중...",
                "timestamp": datetime.now().isoformat(),
            })
            shorts_path = None
            _dbg_log = Path(WORK_DIR) / "step7_debug.log"
            try:
                _dbg = f"Step 7 체크: lv={len(laundered_videos) if laundered_videos else 0}, script={type(job.get('shorts_script'))}, has_script={bool(job.get('shorts_script'))}\n"
                _dbg_log.write_text(_dbg, encoding="utf-8")
                job["results"]["step7_debug"] = _dbg.strip()
                if laundered_videos and job.get("shorts_script"):
                    from affiliate_system.video_launderer import (
                        EmotionTTSEngine, SubtitleGenerator, ShortsRenderer,
                        ProShortsRenderer, _detect_bgm_genre,
                    )
                    from affiliate_system.config import V2_TTS_DIR, V2_SUBTITLE_DIR, V2_SHORTS_DIR

                    script = job["shorts_script"]
                    # list 또는 {"scenes": [...]} 둘 다 지원
                    if isinstance(script, list):
                        scenes_data = script
                    elif isinstance(script, dict):
                        scenes_data = script.get("scenes", [])
                    else:
                        scenes_data = []

                    _dbg_log.write_text(f"Step7 진입: scenes={len(scenes_data)}, lv={len(laundered_videos)}\n", encoding="utf-8")

                    # emotion 유효성 검증
                    valid_emotions = {"excited", "friendly", "urgent", "dramatic", "calm", "hyped"}
                    for sd in scenes_data:
                        emo = sd.get("emotion", "friendly")
                        if emo not in valid_emotions:
                            sd["emotion"] = "friendly"

                    # TTS 생성
                    _dbg_log.write_text(f"TTS 시작...\n", encoding="utf-8")
                    tts_engine = EmotionTTSEngine()
                    scenes = tts_engine.generate_scenes_tts(scenes_data, job_id)
                    _dbg_log.write_text(f"TTS 완료: {len(scenes)}장면\n", encoding="utf-8")

                    # 자막 생성
                    sub_gen = SubtitleGenerator()
                    subtitle_path = sub_gen.generate_ass_from_scenes(scenes, job_id)
                    if not subtitle_path:
                        subtitle_path = str(V2_SUBTITLE_DIR / f"{job_id}_subtitle.ass")
                    _dbg_log.write_text(f"자막: {subtitle_path}\n", encoding="utf-8")

                    # 세탁된 영상을 scene에 매핑 (round-robin 순환)
                    render_scenes = []
                    for i, sc in enumerate(scenes):
                        video_idx = i % len(laundered_videos)
                        video_path = laundered_videos[video_idx]
                        render_scenes.append({
                            "video_clip_path": video_path,
                            "tts_path": sc.get("tts_path", "") or "",
                            "tts_duration": sc.get("tts_duration", sc.get("duration", 3.0)),
                            "text": sc.get("text", ""),
                            "emotion": sc.get("emotion", "friendly"),
                        })

                    # 최종 렌더링 — ProShortsRenderer V3 (모션+전환+BGM+컬러그레이딩)
                    _dbg_log.write_text(f"ProShortsRenderer 시작: {len(render_scenes)}장면\n", encoding="utf-8")
                    product_name = job.get("product_name", topic)
                    category = job.get("category", "")
                    try:
                        renderer = ProShortsRenderer()
                        result_path = renderer.render_pro_shorts(
                            scenes=render_scenes,
                            campaign_id=job_id,
                            subtitle_path=subtitle_path,
                            product_name=product_name,
                            category=category,
                        )
                    except Exception as pro_err:
                        _dbg_log.write_text(f"ProShortsRenderer 실패, 폴백: {pro_err}\n", encoding="utf-8")
                        renderer = ShortsRenderer()
                        result_path = renderer.render_final_shorts(
                            scenes=render_scenes,
                            campaign_id=job_id,
                            subtitle_path=subtitle_path,
                            coupang_link=affiliate_link,
                        )
                    _dbg_log.write_text(f"렌더링 결과: {result_path}\n", encoding="utf-8")
                    if result_path:
                        shorts_path = result_path

                    job["events"].put({
                        "type": "v2_step", "step": 7, "name": "shorts_render",
                        "status": "complete",
                        "detail": f"숏폼 렌더링 완료: {Path(shorts_path).name}" if shorts_path else "렌더링 실패",
                        "timestamp": datetime.now().isoformat(),
                    })
                else:
                    skip_reason = f"laundered={len(laundered_videos) if laundered_videos else 0}, script={bool(job.get('shorts_script'))}"
                    job["results"]["shorts_skip"] = skip_reason
                    job["events"].put({
                        "type": "v2_step", "step": 7, "name": "shorts_render",
                        "status": "complete", "detail": f"숏폼 스킵: {skip_reason}",
                        "timestamp": datetime.now().isoformat(),
                    })
            except Exception as render_err:
                import traceback
                err_detail = traceback.format_exc()
                print(f"[V2] Step 7 숏폼 렌더링 에러: {render_err}")
                print(err_detail)
                job["results"]["shorts_error"] = f"{render_err}\n{err_detail}"
                job["events"].put({
                    "type": "v2_step", "step": 7, "name": "shorts_render",
                    "status": "error", "detail": str(render_err),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 8: 썸네일
            job["events"].put({
                "type": "v2_step", "step": 8, "name": "thumbnail",
                "status": "running", "detail": "플랫폼별 썸네일 생성 중...",
                "timestamp": datetime.now().isoformat(),
            })
            try:
                # 썸네일은 V1 파이프라인 재사용
                job["events"].put({
                    "type": "v2_step", "step": 8, "name": "thumbnail",
                    "status": "complete", "detail": "썸네일 생성 완료 (또는 생략)",
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as te:
                job["events"].put({
                    "type": "v2_step", "step": 8, "name": "thumbnail",
                    "status": "error", "detail": str(te),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 9: 플랫폼별 자동 업로드 (ON/OFF 스위치 기반)
            upload_youtube = job.get("upload_youtube", False)
            upload_instagram = job.get("upload_instagram", False)
            upload_naver = job.get("upload_naver", False)
            any_upload = upload_youtube or upload_instagram or upload_naver

            job["events"].put({
                "type": "v2_step", "step": 9, "name": "upload_ready",
                "status": "running",
                "detail": f"업로드: YT={'ON' if upload_youtube else 'OFF'} | IG={'ON' if upload_instagram else 'OFF'} | Blog={'ON' if upload_naver else 'OFF'}",
                "timestamp": datetime.now().isoformat(),
            })
            upload_results = {}
            try:
                job["results"]["blog_html"] = blog_html
                job["results"]["blog_images"] = blog_images
                job["results"]["shorts_path"] = shorts_path
                job["results"]["laundered_videos"] = laundered_videos

                # 플랫폼별 자동 업로드 실행
                if any_upload:
                    try:
                        from affiliate_system.auto_uploader import StealthUploader
                        uploader = StealthUploader()
                        uploaded = []

                        if upload_youtube and shorts_path:
                            try:
                                if uploader.youtube_auth():
                                    yt_result = uploader.youtube_upload_v2(
                                        video_path=shorts_path,
                                        title=f"{product_title} 추천 #Shorts",
                                        description=f"#{product_title} #쿠팡 #추천 #쇼츠",
                                    )
                                    if yt_result:
                                        uploaded.append("YouTube")
                                        upload_results["youtube"] = yt_result
                            except Exception as yt_err:
                                upload_results["youtube_error"] = str(yt_err)

                        if upload_instagram and shorts_path:
                            try:
                                if uploader.instagram_auth():
                                    ig_result = uploader.instagram_upload_reel_v2(
                                        video_path=shorts_path,
                                        caption=f"{product_title} 솔직 추천! 💯\n#쿠팡 #{product_title.replace(' ', '')} #추천",
                                    )
                                    if ig_result:
                                        uploaded.append("Instagram")
                                        upload_results["instagram"] = ig_result
                            except Exception as ig_err:
                                upload_results["instagram_error"] = str(ig_err)

                        if upload_naver and blog_html:
                            try:
                                naver_result = uploader.naver_blog_post_v2(
                                    html_content=blog_html,
                                    title=product_title,
                                )
                                if naver_result:
                                    uploaded.append("Naver")
                                    upload_results["naver"] = naver_result
                            except Exception as nv_err:
                                upload_results["naver_error"] = str(nv_err)

                        upload_detail = f"업로드 완료: {', '.join(uploaded)}" if uploaded else "업로드 대상 없음"
                    except Exception as up_err:
                        upload_detail = f"업로더 로드 실패: {up_err}"
                else:
                    upload_detail = "자동 업로드 OFF — 결과물 확인 후 수동 업로드"

                job["results"]["upload_results"] = upload_results

                job["events"].put({
                    "type": "v2_step", "step": 9, "name": "upload_ready",
                    "status": "complete",
                    "detail": upload_detail,
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as ue:
                job["events"].put({
                    "type": "v2_step", "step": 9, "name": "upload_ready",
                    "status": "error", "detail": str(ue),
                    "timestamp": datetime.now().isoformat(),
                })

            # Step 10: Drive 아카이빙
            job["events"].put({
                "type": "v2_step", "step": 10, "name": "drive_archive",
                "status": "running", "detail": "Google Drive 아카이빙 중...",
                "timestamp": datetime.now().isoformat(),
            })
            try:
                from affiliate_system.drive_manager import DriveArchiver
                archiver = DriveArchiver()
                if archiver.authenticate():
                    # V2 플랫폼별 파일 분류 — 바로 클릭해서 볼 수 있는 구조
                    valid_images = [p for p in blog_images if p and Path(p).exists()]
                    drive_files = {
                        # 네이버블로그: 블로그 HTML + 이미지
                        "naver_blog": [],
                        # 인스타그램숏츠: 숏폼 영상 (동일 영상 공유)
                        "instagram_shorts": [],
                        # 유튜브숏츠: 숏폼 영상 (동일 영상 공유)
                        "youtube_shorts": [],
                    }

                    # 네이버블로그 폴더: HTML + 이미지
                    if blog_html:
                        blog_html_path = Path(WORK_DIR) / f"blog_{job_id}.html"
                        blog_html_path.write_text(blog_html, encoding="utf-8")
                        drive_files["naver_blog"].append(str(blog_html_path))
                    drive_files["naver_blog"].extend(valid_images)

                    # 숏폼 영상 → 인스타그램 + 유튜브 양쪽에 업로드
                    if shorts_path and Path(shorts_path).exists():
                        drive_files["instagram_shorts"].append(shorts_path)
                        drive_files["youtube_shorts"].append(shorts_path)

                    # 세탁된 원본 영상도 유튜브 폴더에 추가 (편집용 소스)
                    for lv in laundered_videos:
                        if lv and Path(lv).exists():
                            drive_files["youtube_shorts"].append(lv)

                    # 임시 Campaign 객체 생성 — 재스크래핑 않고 저장된 정보 사용
                    from affiliate_system.models import Campaign, AIContent, CampaignStatus, Product
                    temp_product = Product(
                        title=product_title,
                        description=product_info.get("description", ""),
                        url=coupang_link,
                        affiliate_link=affiliate_link,
                    )
                    temp_campaign = Campaign(
                        id=job_id, product=temp_product,
                        ai_content=AIContent(platform_contents={}),
                        status=CampaignStatus.COMPLETE,
                        target_platforms=[],
                        platform_videos={}, platform_thumbnails={},
                        created_at=datetime.now(),
                    )
                    archive_result = archiver.archive_campaign(
                        temp_campaign, drive_files, v2=True
                    )
                    if archive_result["ok"]:
                        job["results"]["drive_url"] = archive_result.get("folder_url", "")
                        job["results"]["drive_platforms"] = archive_result.get("platform_urls", {})
                        job["events"].put({
                            "type": "v2_step", "step": 10, "name": "drive_archive",
                            "status": "complete",
                            "detail": f"Drive 아카이빙 완료: {archive_result['files_uploaded']}개 파일 (3 플랫폼)",
                            "timestamp": datetime.now().isoformat(),
                        })
                    else:
                        job["events"].put({
                            "type": "v2_step", "step": 10, "name": "drive_archive",
                            "status": "error", "detail": "Drive 업로드 일부 실패",
                            "timestamp": datetime.now().isoformat(),
                        })
                else:
                    job["events"].put({
                        "type": "v2_step", "step": 10, "name": "drive_archive",
                        "status": "error", "detail": "Drive 인증 실패",
                        "timestamp": datetime.now().isoformat(),
                    })
            except Exception as de:
                job["events"].put({
                    "type": "v2_step", "step": 10, "name": "drive_archive",
                    "status": "error", "detail": str(de),
                    "timestamp": datetime.now().isoformat(),
                })

            # 완료
            job["state"] = V2PipelineState.COMPLETE
            job["events"].put({
                "type": "v2_complete",
                "message": "🎉 V2 파이프라인 10단계 완료!",
                "results": _safe_serialize(job["results"]),
                "timestamp": datetime.now().isoformat(),
            })

            # 캠페인 DB 저장
            _save_campaign(
                job_id, product_info.get("title", "V2 Campaign"),
                "V2", job["platforms"], "complete",
                results=_safe_serialize(job["results"]),
            )

        except Exception as e:
            job["state"] = V2PipelineState.ERROR
            job["error"] = str(e)
            job["events"].put({
                "type": "error", "error": str(e),
                "timestamp": datetime.now().isoformat(),
            })

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "state": V2PipelineState.EXECUTING})


@app.route('/api/v2/campaign/<job_id>/status')
def v2_campaign_status(job_id):
    """V2 캠페인 상태 조회."""
    job = v2_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "job_id": job_id,
        "state": job["state"],
        "coupang_link": job.get("coupang_link"),
        "product_info": job.get("product_info"),
        "draft": job.get("draft"),
        "blog_html": job.get("blog_html", ""),
        "results": _safe_serialize(job.get("results", {})),
        "error": job.get("error"),
        "created_at": job.get("created_at"),
    })


@app.route('/api/v2/campaign/<job_id>/stream')
def v2_stream(job_id):
    """V2 SSE 스트리밍 — 10단계 진행상황."""
    def generate():
        job = v2_jobs.get(job_id)
        if not job:
            yield f"data: {json.dumps({'type': 'error', 'error': 'Job not found'})}\n\n"
            return

        q = job["events"]
        while job["state"] not in (V2PipelineState.COMPLETE, V2PipelineState.ERROR):
            while not q.empty():
                event = q.get_nowait()
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            time.sleep(0.3)

        # 잔여 이벤트 flush
        while not q.empty():
            event = q.get_nowait()
            yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        # 최종 상태
        if job["state"] == V2PipelineState.COMPLETE:
            yield f"data: {json.dumps({'type': 'v2_done', 'results': _safe_serialize(job.get('results', {}))}, ensure_ascii=False, default=str)}\n\n"
        elif job["state"] == V2PipelineState.ERROR:
            yield f"data: {json.dumps({'type': 'error', 'error': job.get('error', 'Unknown error')})}\n\n"

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.route('/api/v2/campaign/<job_id>/blog-preview')
def v2_blog_preview(job_id):
    """V2 블로그 HTML 미리보기."""
    job = v2_jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    blog_html = job.get("blog_html", "")
    if blog_html:
        return Response(blog_html, mimetype='text/html; charset=utf-8')
    return jsonify({"error": "블로그 HTML 아직 생성되지 않음"}), 404


# ═══════════════════════════════════════════════════════════════
# 서버 실행
# ═══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    print("=" * 50)
    print("YJ MCN Automation Dashboard Server")
    print(f"  URL: http://localhost:5001")
    print(f"  Gemini: {'OK' if GEMINI_API_KEY else 'NO'}")
    print(f"  Claude: {'OK' if ANTHROPIC_API_KEY else 'NO'}")
    print(f"  Pexels: {'OK' if PEXELS_API_KEY else 'NO'}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
