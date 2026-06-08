from __future__ import annotations

from collections.abc import Callable
from typing import Any

from flask import Flask, jsonify, render_template, request

from .preferences import PreferenceAnalyzer
from .recommendations import RecommendationService
from .settings import Settings
from .storage_db import Database


def register_preference_routes(app: Flask, settings: Settings | Callable[[], Settings]) -> None:
    def current_settings() -> Settings:
        return settings() if callable(settings) else settings

    def ok(data: Any = None):
        return jsonify({"ok": True, "data": data})

    def fail(exc: Exception):
        return jsonify({"ok": False, "error": str(exc)}), 400

    def json_payload() -> dict[str, Any]:
        return request.get_json(silent=True) or {}

    def db() -> Database:
        instance = Database(current_settings().storage.db_path)
        instance.init_schema()
        return instance

    @app.get("/dashboard/preferences")
    def dashboard_preferences_page():
        return render_template("dashboard_preferences.html")

    @app.get("/api/dashboard/preferences/profiles")
    def list_preference_profiles():
        instance = db()
        try:
            return ok(instance.list_preference_profiles())
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.get("/api/dashboard/preferences/profiles/<int:profile_id>")
    def get_preference_profile(profile_id: int):
        instance = db()
        try:
            profile = instance.get_preference_profile(profile_id)
            if not profile:
                return jsonify({"ok": False, "error": "偏好画像不存在"}), 404
            return ok(profile)
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/preferences/profiles/analyze")
    def analyze_preference_profile():
        payload = json_payload()
        instance = db()
        try:
            analyzer = PreferenceAnalyzer(instance)
            result = analyzer.analyze_local(payload.get("scope") or {})
            profile_id = instance.create_preference_profile({
                "name": payload.get("name") or "本地偏好画像",
                "description": payload.get("description") or "基于本地归档小说自动统计生成",
                "source_scope": result["source_scope"],
                "stats": result["stats"],
                "profile": result["profile"],
                "is_default": bool(payload.get("is_default", True)),
            })
            return ok(instance.get_preference_profile(profile_id))
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.put("/api/dashboard/preferences/profiles/<int:profile_id>")
    def update_preference_profile(profile_id: int):
        instance = db()
        try:
            instance.update_preference_profile(profile_id, json_payload())
            return ok(instance.get_preference_profile(profile_id))
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/preferences/profiles/<int:profile_id>/default")
    def set_default_preference_profile(profile_id: int):
        instance = db()
        try:
            if not instance.get_preference_profile(profile_id):
                return jsonify({"ok": False, "error": "偏好画像不存在"}), 404
            instance.set_default_preference_profile(profile_id)
            return ok()
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.delete("/api/dashboard/preferences/profiles/<int:profile_id>")
    def delete_preference_profile(profile_id: int):
        instance = db()
        try:
            instance.delete_preference_profile(profile_id)
            return ok()
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/recommendations/search-plan")
    def build_recommendation_search_plan():
        payload = json_payload()
        instance = db()
        try:
            profile_id = payload.get("profile_id")
            profile = instance.get_preference_profile(int(profile_id)) if profile_id else instance.get_default_preference_profile()
            if not profile:
                return jsonify({"ok": False, "error": "需要先生成偏好画像"}), 400
            service = RecommendationService(instance, current_settings())
            return ok(service.build_search_plan(profile, payload.get("filters") or {}))
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/recommendations/run")
    def run_recommendations():
        payload = json_payload()
        instance = db()
        try:
            service = RecommendationService(instance, current_settings())
            result = service.run(
                profile_id=int(payload["profile_id"]) if payload.get("profile_id") else None,
                search_plan=payload.get("search_plan"),
            )
            return ok(result)
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.get("/api/dashboard/recommendations/runs")
    def list_recommendation_runs():
        instance = db()
        try:
            limit = int(request.args.get("limit") or 20)
            return ok(instance.list_recommendation_runs(limit=limit))
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.get("/api/dashboard/recommendations/items")
    def list_recommendation_items():
        instance = db()
        try:
            status = request.args.get("status") or None
            limit = int(request.args.get("limit") or 100)
            return ok(instance.list_recommendation_items(status=status, limit=limit))
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/recommendations/items/<int:item_id>/feedback")
    def create_recommendation_feedback(item_id: int):
        payload = json_payload()
        instance = db()
        try:
            item = instance.get_recommendation_item(item_id)
            if not item:
                return jsonify({"ok": False, "error": "推荐项不存在"}), 404
            feedback_type = str(payload.get("feedback_type") or "").strip()
            if not feedback_type:
                return jsonify({"ok": False, "error": "缺少 feedback_type"}), 400
            instance.create_recommendation_feedback({
                "item_type": item["item_type"],
                "novel_id": item.get("novel_id"),
                "series_id": item.get("series_id"),
                "author_id": item.get("author_id"),
                "feedback_type": feedback_type,
                "note": payload.get("note"),
            })
            if feedback_type in {"interested", "dismissed", "saved", "muted"}:
                instance.update_recommendation_item_status(item_id, feedback_type)
            return ok()
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.get("/api/dashboard/recommendations/mutes")
    def list_recommendation_mutes():
        instance = db()
        try:
            return ok(instance.list_recommendation_mutes())
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.post("/api/dashboard/recommendations/mutes")
    def create_recommendation_mute():
        payload = json_payload()
        instance = db()
        try:
            mute_type = str(payload.get("mute_type") or "").strip()
            mute_value = str(payload.get("mute_value") or "").strip()
            if mute_type not in {"author", "tag"} or not mute_value:
                return jsonify({"ok": False, "error": "屏蔽类型或值无效"}), 400
            mute_id = instance.create_recommendation_mute(mute_type, mute_value, payload.get("reason"))
            return ok({"id": mute_id})
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()

    @app.delete("/api/dashboard/recommendations/mutes/<int:mute_id>")
    def delete_recommendation_mute(mute_id: int):
        instance = db()
        try:
            instance.delete_recommendation_mute(mute_id)
            return ok()
        except Exception as exc:
            return fail(exc)
        finally:
            instance.close()
