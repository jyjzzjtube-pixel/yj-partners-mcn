# -*- coding: utf-8 -*-
"""
YJ MCN 자동화 대시보드 — Flask 백엔드
=====================================
기존 affiliate_system 모듈을 REST API로 래핑.
SSE로 파이프라인 진행상황 실시간 스트리밍.

실행: python yj-partners-mcn/mcn_server.py
"""
import json
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
    PIXABAY_API_KEY, UNSPLASH_ACCESS_KEY, RENDER_OUTPUT_DIR,
)
from affiliate_system.models import Platform, PLATFORM_PRESETS

# ── Flask 앱 설정 ──
app = Flask(__name__, static_folder=str(Path(__file__).parent))
CORS(app)

# ── Job 저장소 ──
jobs = {}  # job_id -> {status, step, progress, results, events, error}

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

def serialize_results(results: dict) -> dict:
    """파이프라인 결과를 JSON 직렬화 가능한 형태로 변환."""
    out = {"platforms": {}, "upload_results": {}}
    for p_name, p_data in results.get("platforms", {}).items():
        out["platforms"][p_name] = {
            "video": str(p_data.get("video", "")) if p_data.get("video") else None,
            "thumbnail": str(p_data.get("thumbnail", "")) if p_data.get("thumbnail") else None,
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

    def run(self, topic: str, platforms: list, brand: str, persona: str, auto_upload: bool) -> dict:
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

        # 결과 조합
        for p in platform_enums:
            p_name = p.value
            results["platforms"][p_name] = {
                "video": str(videos.get(p_name, "")) if videos.get(p_name) else None,
                "thumbnail": str(thumbnails.get(p_name, "")) if thumbnails.get(p_name) else None,
                "content": platform_contents.get(p_name, {}),
            }

        # Step 6: 업로드
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

        results["campaign_id"] = campaign_id
        return results


# ═══════════════════════════════════════════════════════════════
# API 엔드포인트
# ═══════════════════════════════════════════════════════════════

# ── 메인 페이지 ──
@app.route('/')
def index():
    return send_file(str(Path(__file__).parent / 'index.html'))


# ── 건강 체크 ──
@app.route('/api/health')
def health():
    services = {
        "gemini": bool(GEMINI_API_KEY),
        "claude": bool(ANTHROPIC_API_KEY),
        "pexels": bool(PEXELS_API_KEY),
        "pixabay": bool(PIXABAY_API_KEY),
        "unsplash": bool(UNSPLASH_ACCESS_KEY),
    }
    return jsonify({
        "status": "online",
        "services": services,
        "active_jobs": sum(1 for j in jobs.values() if j["status"] == "running"),
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

    def worker():
        job = jobs[job_id]
        job["status"] = "running"
        try:
            wp = WebPipeline(events_queue)
            results = wp.run(topic, platforms, brand, persona, auto_upload)
            job["results"] = results
            job["status"] = "complete"
            events_queue.put({"type": "complete", "results": results})
        except Exception as e:
            job["error"] = str(e)
            job["status"] = "error"
            events_queue.put({"type": "error", "error": str(e)})

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


# ── 비용 현황 ──
@app.route('/api/cost')
def get_cost():
    try:
        from api_cost_tracker import CostTracker
        tracker = CostTracker()
        return jsonify(tracker.get_summary())
    except Exception:
        return jsonify({"total_usd": 0, "note": "비용 추적기 없음"})


# ── 파일 다운로드 (렌더링된 영상/이미지) ──
@app.route('/api/file/<path:filepath>')
def serve_file(filepath):
    full_path = PROJECT_DIR / filepath
    if full_path.exists() and full_path.is_file():
        return send_file(str(full_path))
    return jsonify({"error": "파일 없음"}), 404


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
