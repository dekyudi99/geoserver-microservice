import os
import re
import uuid
import datetime
import logging
import rasterio
import numpy as np
from typing import Optional, List, Dict, Any, Literal

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ...services.style_service import style_service
from ...services.raster_stats_service import compute_raster_stats, classify, AllMaskedError
from ...models.spatial_data import RasterMetadata, VectorLayer
from ...models.api_key import ApiKey, ApiKeyType
from ...security.auth import require_any_key
from ...config.database import get_db
from ...services.audit_service import log_action

logger = logging.getLogger("geoserver_service.v1_style")

ASSETS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "assets"))

router = APIRouter(prefix="/styles", tags=["Styles"])

ClassificationMethodEnum = Literal[
    "natural_breaks",
    "equal_interval",
    "quantile",
    "unique_values",
    "manual",
    "preset"
]

StyleTypeEnum = Literal["intervals", "values", "ramp"]

class StyleApplyPayload(BaseModel):
    layer_kind: Optional[str] = None
    workspace_name: Optional[str] = None
    layer_name: Optional[str] = None
    layer_id: Optional[str] = None
    style_name: Optional[str] = None
    sld_xml: Optional[str] = None
    style_sld: Optional[str] = None
    style_type: Optional[StyleTypeEnum] = "intervals"
    colors: Optional[List[Dict[str, Any]]] = None
    classification_method: Optional[ClassificationMethodEnum] = None
    classes_count: Optional[int] = Field(default=None, ge=1, le=256)
    color_ramp: Optional[str] = None
    last_auto_method: Optional[ClassificationMethodEnum] = None
    preset_id: Optional[str] = None
    breaks: Optional[List[float]] = None
    label_edited: Optional[List[bool]] = None
    geometry_type: Optional[str] = None
    style_mode: Optional[str] = None
    symbol: Optional[Dict[str, Any]] = None

    @field_validator("classification_method", "last_auto_method", mode="before")
    @classmethod
    def normalize_methods(cls, v):
        if v is None:
            return None
        s = str(v).strip().lower()
        if s == "custom":
            return "manual"
        if s == "jenks":
            return "natural_breaks"
        return s

class RasterClassifyPayload(BaseModel):
    method: Literal["natural_breaks", "quantile", "equal_interval"]
    classes: int = Field(..., ge=1, le=50)

    @field_validator("method", mode="before")
    @classmethod
    def normalize_method(cls, v):
        if v is None:
            return v
        s = str(v).strip().lower()
        if s == "jenks":
            return "natural_breaks"
        return s

def get_raster_or_404_403(
    layer_id_or_name: str,
    db: Session,
    caller: ApiKey,
    workspace_name: Optional[str] = None
) -> RasterMetadata:
    raster_obj = None
    try:
        lid = uuid.UUID(str(layer_id_or_name))
        raster_obj = db.query(RasterMetadata).filter(RasterMetadata.id == lid).first()
    except (ValueError, TypeError, AttributeError):
        pass

    if not raster_obj:
        query = db.query(RasterMetadata).filter(
            (RasterMetadata.store_name == layer_id_or_name) | (RasterMetadata.layer_name == layer_id_or_name)
        )
        if workspace_name:
            query = query.filter(RasterMetadata.workspace_name == workspace_name)
        raster_obj = query.first()

    if not raster_obj:
        raise HTTPException(status_code=404, detail="Raster tidak ditemukan.")

    if raster_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
        raise HTTPException(status_code=403, detail="Anda tidak memiliki izin mengakses raster ini.")

    return raster_obj

@router.post("/apply", status_code=status.HTTP_200_OK)
def apply_style(
    req: StyleApplyPayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    try:
        provided_sld = req.sld_xml or req.style_sld
        if req.layer_kind not in (None, "raster", "vector"):
            raise HTTPException(status_code=422, detail="layer_kind harus 'raster' atau 'vector'.")
        raster_obj = None
        vector_obj = None
        layer_uuid = None
        if req.layer_id:
            try:
                layer_uuid = uuid.UUID(str(req.layer_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise HTTPException(status_code=400, detail="layer_id tidak valid.") from exc

        target_name = str(layer_uuid) if layer_uuid else req.layer_name
        if req.layer_kind == "raster":
            if not target_name:
                raise HTTPException(status_code=400, detail="layer_id atau layer_name harus diisi.")
            raster_obj = get_raster_or_404_403(target_name, db, caller, workspace_name=req.workspace_name)
            ws_name = raster_obj.workspace_name
            lyr_name = raster_obj.store_name
            layer_kind = "raster"
            layer_uuid = raster_obj.id
        else:
            if target_name and req.layer_kind != "vector":
                try:
                    raster_obj = get_raster_or_404_403(target_name, db, caller, workspace_name=req.workspace_name)
                except HTTPException as e:
                    if e.status_code == 403:
                        raise
                    raster_obj = None

            if raster_obj:
                ws_name = raster_obj.workspace_name
                lyr_name = raster_obj.store_name
                layer_kind = "raster"
                layer_uuid = raster_obj.id
            else:
                if layer_uuid:
                    vector_obj = db.query(VectorLayer).filter(VectorLayer.id == layer_uuid).first()
                elif req.layer_name and req.workspace_name and req.layer_kind in (None, "vector"):
                    vector_obj = db.query(VectorLayer).filter(
                        VectorLayer.workspace_name == req.workspace_name,
                        (VectorLayer.table_name == req.layer_name) | (VectorLayer.layer_name == req.layer_name),
                    ).first()
                if not vector_obj:
                    raise HTTPException(status_code=404, detail="Layer tidak ditemukan.")
                if vector_obj.api_key_id != caller.id and caller.key_type != ApiKeyType.PRIMARY:
                    raise HTTPException(status_code=403, detail="Anda tidak memiliki izin mengubah style vector ini.")
                if req.layer_kind == "raster":
                    raise HTTPException(status_code=400, detail="Tipe layer tidak cocok: layer_id ini adalah vector.")
                ws_name = vector_obj.workspace_name
                lyr_name = vector_obj.table_name
                layer_uuid = vector_obj.id
                layer_kind = "vector"

        if raster_obj:
            layer_uuid = raster_obj.id

        st_name = req.style_name or f"style_{lyr_name}"
        if len(st_name) > 100 or not st_name.replace("_", "").replace("-", "").isalnum():
            raise HTTPException(status_code=400, detail="style_name hanya boleh berisi huruf, angka, '-' atau '_'.")

        if layer_kind == "raster":
            if req.symbol:
                raise HTTPException(status_code=400, detail="Raster menerima colors, bukan symbol vector.")
            if provided_sld:
                if caller.key_type != ApiKeyType.PRIMARY:
                    raise HTTPException(
                        status_code=403,
                        detail="Penyediaan sld_xml mentah untuk raster hanya diizinkan untuk PRIMARY key. Gunakan 'colors' untuk menghasilkan SLD di server."
                    )
                parsed_check = style_service.parse_raster_sld(provided_sld)
                if not parsed_check or not parsed_check.get("entries"):
                    raise HTTPException(
                        status_code=422,
                        detail="sld_xml raster tidak valid atau tidak memiliki ColorMap yang valid."
                    )
                sld_xml = provided_sld
            else:
                if not req.colors:
                    raise HTTPException(status_code=400, detail="Raster memerlukan colors untuk membuat SLD.")
                if len(req.colors) > 256:
                    raise HTTPException(status_code=400, detail="Style raster maksimal memiliki 256 kelas.")
                sld_xml = style_service.generate_raster_sld(
                    style_name=st_name,
                    color_entries=req.colors,
                    style_type=req.style_type or "intervals"
                )
        else:
            if req.colors:
                raise HTTPException(status_code=400, detail="Vector tidak menerima colors.")
            geometry_type = (vector_obj.geom_type or "").strip()
            symbol = req.symbol or {}

            if provided_sld:
                sld_xml = provided_sld
            else:
                if not symbol:
                    raise HTTPException(status_code=400, detail="Vector memerlukan symbol atau sld_xml.")
                normalized_geometry = geometry_type.lower().replace(" ", "")
                requested_geometry = (req.geometry_type or geometry_type).lower().replace(" ", "")
                if requested_geometry != normalized_geometry:
                    raise HTTPException(status_code=400, detail="geometry_type tidak cocok dengan metadata layer.")
                allowed_symbol_fields = {
                    "fill_color", "fill_opacity", "stroke_color", "stroke_width",
                    "stroke_opacity", "point_size", "mark", "stroke_dasharray",
                    "marker_type", "icon_name", "icon_url"
                }
                unknown_fields = set(symbol) - allowed_symbol_fields
                if unknown_fields:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Properti symbol tidak didukung: {', '.join(sorted(unknown_fields))}.",
                    )
                if req.style_mode not in (None, "single"):
                    raise HTTPException(status_code=400, detail="Vector saat ini hanya mendukung style_mode 'single'.")
                try:
                    sld_xml = style_service.generate_vector_sld(
                        style_name=st_name,
                        geom_type=geometry_type,
                        fill_color=symbol.get("fill_color", "#0d9488"),
                        stroke_color=symbol.get("stroke_color", "#0f766e"),
                        stroke_width=symbol.get("stroke_width", 1.5),
                        fill_opacity=symbol.get("fill_opacity", 0.45),
                        stroke_opacity=symbol.get("stroke_opacity", 1.0),
                        point_size=symbol.get("point_size", 8),
                        mark=symbol.get("mark", "circle"),
                        stroke_dasharray=symbol.get("stroke_dasharray"),
                        icon_name=symbol.get("icon_name"),
                        icon_url=symbol.get("icon_url"),
                    )
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc

        apply_res = style_service.apply_style(
            workspace=ws_name,
            layer_name=lyr_name,
            style_name=st_name,
            sld_xml=sld_xml
        )
        if not apply_res.get("success"):
            err_msg = apply_res.get("message") or "GeoServer gagal menerapkan style ke layer."
            raise HTTPException(status_code=502, detail=f"GeoServer gagal menerapkan style: {err_msg}")

        updated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if layer_kind == "raster":
            parsed_sld = style_service.parse_raster_sld(sld_xml) if sld_xml else None
            raw_classes = req.colors or (parsed_sld.get("entries") if parsed_sld else None)
            st_type = req.style_type or (parsed_sld.get("style_type") if parsed_sld else "intervals")

            raw_method = req.classification_method or "manual"
            if raw_method == "custom":
                norm_method = "manual"
            elif raw_method == "jenks":
                norm_method = "natural_breaks"
            else:
                norm_method = raw_method

            enriched_classes = []
            if raw_classes:
                for idx, c in enumerate(raw_classes):
                    q = c.get("quantity")
                    if q is None:
                        q = c.get("max", 0.0)
                    try:
                        q = float(q)
                    except (ValueError, TypeError):
                        q = 0.0

                    c_min = c.get("min")
                    c_max = c.get("max")
                    if c_min is None:
                        if req.breaks and len(req.breaks) >= len(raw_classes) + 1:
                            c_min = req.breaks[idx]
                        elif idx == 0:
                            c_min = q
                        else:
                            prev_q = raw_classes[idx - 1].get("quantity", raw_classes[idx - 1].get("max", 0.0))
                            c_min = prev_q
                    if c_max is None:
                        if req.breaks and len(req.breaks) >= len(raw_classes) + 1:
                            c_max = req.breaks[idx + 1]
                        else:
                            c_max = q

                    try:
                        c_min = float(c_min)
                    except (ValueError, TypeError):
                        c_min = q
                    try:
                        c_max = float(c_max)
                    except (ValueError, TypeError):
                        c_max = q

                    lbl_edited = False
                    if req.label_edited and idx < len(req.label_edited):
                        lbl_edited = bool(req.label_edited[idx])
                    elif "label_edited" in c:
                        lbl_edited = bool(c.get("label_edited"))

                    lbl = c.get("label")
                    if not lbl:
                        if st_type == "values":
                            lbl = f"Value {q}"
                        else:
                            lbl = f"{c_min} - {c_max}"

                    op = c.get("opacity", 1.0)
                    try:
                        op = float(op)
                    except (ValueError, TypeError):
                        op = 1.0

                    enriched_classes.append({
                        "min": c_min,
                        "max": c_max,
                        "quantity": q,
                        "color": c.get("color", "#000000"),
                        "opacity": op,
                        "label": str(lbl),
                        "label_edited": lbl_edited
                    })

                raster_obj.symbology = {
                    "layer_kind": "raster",
                    "version": 2,
                    "style_name": st_name,
                    "style_type": st_type,
                    "classification_method": norm_method,
                    "last_auto_method": req.last_auto_method,
                    "classes_count": req.classes_count or len(enriched_classes),
                    "color_ramp": req.color_ramp or "custom",
                    "preset_id": req.preset_id,
                    "breaks": req.breaks,
                    "classes": enriched_classes,
                    "updated_at": updated_at
                }
        elif layer_kind == "vector":
            vector_obj.symbology = {
                "layer_kind": "vector",
                "style_mode": "single",
                "geometry_type": geometry_type,
                "symbol": {
                    "fill_color": symbol.get("fill_color", "#0d9488"),
                    "fill_opacity": float(symbol.get("fill_opacity", 0.45)),
                    "stroke_color": symbol.get("stroke_color", "#0f766e"),
                    "stroke_width": float(symbol.get("stroke_width", 1.5)),
                    "stroke_opacity": float(symbol.get("stroke_opacity", 1.0)),
                    "point_size": float(symbol.get("point_size", 8)),
                    "mark": symbol.get("mark", "circle"),
                    "stroke_dasharray": symbol.get("stroke_dasharray"),
                    "marker_type": symbol.get("marker_type", "shape"),
                    "icon_name": symbol.get("icon_name"),
                    "icon_url": symbol.get("icon_url"),
                },
                "style_name": st_name,
                "updated_at": updated_at,
            }
        db.commit()

        log_action(
            db=db,
            api_key=caller,
            action="APPLY_STYLE",
            resource_type="STYLE",
            resource_id=str(layer_uuid),
            resource_name=st_name,
            status="SUCCESS"
        )

        return {
            "success": True,
            "layer_id": str(layer_uuid),
            "layer_kind": layer_kind,
            "style_name": st_name,
            "symbology": raster_obj.symbology if layer_kind == "raster" else vector_obj.symbology,
            "detail": f"Style '{st_name}' berhasil diaplikasikan ke layer '{lyr_name}'.",
        }
    except HTTPException:
        db.rollback()
        raise
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        db.rollback()
        logger.exception(f"Gagal menerapkan style: {exc}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan internal pada server.") from exc

@router.post("/raster-classify/{layer_id}", status_code=status.HTTP_200_OK)
def raster_classify(
    layer_id: str,
    req: RasterClassifyPayload,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    raster_obj = get_raster_or_404_403(layer_id, db, caller)

    if req.classes < 1 or req.classes > 50:
        raise HTTPException(status_code=422, detail="classes harus bernilai antara 1 sampai 50.")

    valid_methods = {"natural_breaks", "quantile", "equal_interval"}
    norm_method = "manual" if req.method == "custom" else ("natural_breaks" if req.method == "jenks" else req.method)
    if norm_method not in valid_methods:
        raise HTTPException(
            status_code=422,
            detail=f"method tidak didukung: '{req.method}'. Pilih salah satu dari: {', '.join(sorted(valid_methods))}."
        )

    try:
        res = classify(
            path=raster_obj.file_path,
            method=norm_method,
            classes=req.classes
        )
        return res
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AllMaskedError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (IOError, rasterio.errors.RasterioError) as exc:
        logger.exception(f"Storage I/O error reading raster for classification: {exc}")
        raise HTTPException(status_code=503, detail=f"Gagal membaca file raster dari storage: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception(f"Unexpected error classifying raster {layer_id}: {exc}")
        raise HTTPException(status_code=503, detail=f"Layanan klasifikasi raster tidak tersedia: {exc}") from exc

@router.get("/raster-info/{layer_id}")
def get_raster_info(
    layer_id: str,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    raster_obj = get_raster_or_404_403(layer_id, db, caller)

    stats = None
    statistics_error = None
    try:
        stats = compute_raster_stats(raster_obj.file_path)
    except FileNotFoundError as exc:
        statistics_error = "File raster tidak ditemukan pada storage."
        logger.warning(f"File raster not found for layer {raster_obj.id}: {exc}")
    except AllMaskedError as exc:
        statistics_error = str(exc)
        logger.warning(f"All masked raster {raster_obj.id}: {exc}")
    except Exception as exc:
        statistics_error = "Gagal memproses statistik raster."
        logger.exception(f"Gagal menghitung statistik raster untuk layer {raster_obj.id}: {exc}")

    active_symbology = None
    if isinstance(raster_obj.symbology, dict) and raster_obj.symbology.get("classes"):
        active_symbology = dict(raster_obj.symbology)
        raw_m = active_symbology.get("classification_method")
        if raw_m == "custom":
            active_symbology["classification_method"] = "manual"
        elif raw_m == "jenks":
            active_symbology["classification_method"] = "natural_breaks"

        if active_symbology.get("version") != 2:
            st_type = active_symbology.get("style_type", "intervals")
            if not raw_m:
                active_symbology["classification_method"] = "unique_values" if st_type == "values" else "manual"

    if not active_symbology:
        # 1. Try detecting active SLD style from GeoServer
        try:
            from ...clients.geoserver_client import geoserver_client

            layer_info = geoserver_client.get_layer(
                layer_name=raster_obj.store_name,
                workspace_name=raster_obj.workspace_name
            ) or geoserver_client.get_layer(layer_name=raster_obj.store_name)

            style_name = None
            if layer_info and isinstance(layer_info, dict):
                style_name = layer_info.get("defaultStyle", {}).get("name")

            if style_name and style_name.lower() != "raster":
                sld_path = f"/rest/workspaces/{raster_obj.workspace_name}/styles/{style_name}.sld"
                res = geoserver_client._request("GET", sld_path)
                if res.status_code != 200:
                    sld_path = f"/rest/styles/{style_name}.sld"
                    res = geoserver_client._request("GET", sld_path)

                if res.status_code == 200 and res.text:
                    parsed = style_service.parse_raster_sld(res.text)
                    if parsed and parsed.get("entries"):
                        entries = parsed["entries"]
                        sld_style_type = parsed["style_type"]
                        classes = []
                        min_stat = stats.get("min") if (stats and isinstance(stats, dict)) else None
                        for idx, entry in enumerate(entries):
                            q = entry["quantity"]
                            if sld_style_type == "values":
                                c_min = q
                                c_max = q
                            else:
                                c_min = min_stat if idx == 0 else entries[idx - 1]["quantity"]
                                c_max = q
                            lbl = entry["label"] or (
                                f"Value {q}" if sld_style_type == "values"
                                else (f"{c_min} - {c_max}" if c_min is not None else f"<= {c_max}")
                            )
                            classes.append({
                                "min": c_min,
                                "max": c_max,
                                "quantity": q,
                                "color": entry["color"],
                                "opacity": entry["opacity"],
                                "label": lbl,
                                "label_edited": False
                            })

                        active_symbology = {
                            "layer_kind": "raster",
                            "version": 2,
                            "style_name": style_name,
                            "style_type": sld_style_type,
                            "classification_method": "unique_values" if sld_style_type == "values" else "manual",
                            "last_auto_method": None,
                            "classes_count": len(classes),
                            "color_ramp": "custom",
                            "preset_id": None,
                            "breaks": None,
                            "classes": classes,
                            "updated_at": raster_obj.created_at.isoformat() if raster_obj.created_at else None
                        }
                        if raster_obj.symbology is None:
                            try:
                                raster_obj.symbology = active_symbology
                                db.commit()
                            except Exception as commit_err:
                                db.rollback()
                                logger.warning(f"Gagal commit deteksi SLD ke DB: {commit_err}")
        except Exception as sld_err:
            logger.warning(f"Gagal mendeteksi SLD GeoServer untuk raster {raster_obj.id}: {sld_err}")

    if not active_symbology and raster_obj.file_path:
        # 2. Try detecting embedded colormap in TIFF file
        try:
            with rasterio.open(raster_obj.file_path) as src:
                try:
                    cmap = src.colormap(1)
                except Exception:
                    cmap = None

                if cmap and isinstance(cmap, dict) and len(cmap) > 0:
                    classes = []
                    sorted_keys = sorted(cmap.keys())
                    for k in sorted_keys:
                        rgba = cmap[k]
                        hex_col = f"#{rgba[0]:02x}{rgba[1]:02x}{rgba[2]:02x}"
                        op = round(rgba[3] / 255.0, 2) if len(rgba) > 3 else 1.0
                        classes.append({
                            "min": float(k),
                            "max": float(k),
                            "quantity": float(k),
                            "color": hex_col,
                            "opacity": op,
                            "label": f"Class {k}",
                            "label_edited": False
                        })
                    if classes:
                        active_symbology = {
                            "layer_kind": "raster",
                            "version": 2,
                            "style_name": f"embedded_{raster_obj.store_name}",
                            "style_type": "values",
                            "classification_method": "unique_values",
                            "last_auto_method": None,
                            "classes_count": len(classes),
                            "color_ramp": "custom",
                            "preset_id": None,
                            "breaks": None,
                            "classes": classes,
                            "updated_at": raster_obj.created_at.isoformat() if raster_obj.created_at else None
                        }
                        if raster_obj.symbology is None:
                            try:
                                raster_obj.symbology = active_symbology
                                db.commit()
                            except Exception as commit_err:
                                db.rollback()
                                logger.warning(f"Gagal commit colormap TIFF ke DB: {commit_err}")
        except Exception as tiff_err:
            logger.warning(f"Gagal mendeteksi embedded colormap TIFF untuk raster {raster_obj.id}: {tiff_err}")

    return {
        "id": str(raster_obj.id),
        "workspace_name": raster_obj.workspace_name,
        "layer_name": raster_obj.store_name,
        "statistics": stats,
        "statistics_error": statistics_error,
        "saved_symbology": active_symbology,
        "dimensions": raster_obj.dimensions,
        "bbox": raster_obj.bbox
    }

@router.get("/icons", status_code=status.HTTP_200_OK)
def get_available_icons():
    """
    Mengambil daftar icon SVG yang tersedia di app/assets untuk marker vector layer.
    """
    icons = []
    if os.path.exists(ASSETS_DIR):
        for f in sorted(os.listdir(ASSETS_DIR)):
            if f.lower().endswith(".svg"):
                name_clean = os.path.splitext(f)[0]
                formatted_name = " ".join([
                    word.capitalize() for word in re.split(r"[-_]+", name_clean)
                    if word and word.lower() not in ("svgrepo", "com")
                ])
                if not formatted_name:
                    formatted_name = name_clean.capitalize()
                icons.append({
                    "id": f,
                    "filename": f,
                    "name": formatted_name,
                    "url": f"/assets/{f}"
                })
    return {"total": len(icons), "data": icons}

@router.delete("/{style_name}", status_code=status.HTTP_200_OK)
def delete_style(
    style_name: str,
    workspace: Optional[str] = None,
    recurse: bool = False,
    db: Session = Depends(get_db),
    caller: ApiKey = Depends(require_any_key)
):
    try:
        from ...clients.geoserver_client import geoserver_client

        if caller.key_type != ApiKeyType.PRIMARY:
            # Hanya PRIMARY key, atau pemilik layer yang memakai style itu, yang boleh menghapus.
            raster_candidates = db.query(RasterMetadata).filter(
                (RasterMetadata.store_name == style_name) |
                (RasterMetadata.layer_name == style_name) |
                (RasterMetadata.symbology.isnot(None))
            ).all()

            vector_candidates = db.query(VectorLayer).filter(
                (VectorLayer.table_name == style_name) |
                (VectorLayer.layer_name == style_name) |
                (VectorLayer.symbology.isnot(None))
            ).all()

            owned_match = False
            unowned_match = False

            for r in raster_candidates:
                r_style = (r.symbology.get("style_name") if isinstance(r.symbology, dict) else None)
                if r_style == style_name or f"style_{r.store_name}" == style_name or f"style_{r.layer_name}" == style_name:
                    if r.api_key_id == caller.id:
                        owned_match = True
                    else:
                        unowned_match = True

            for v in vector_candidates:
                v_style = (v.symbology.get("style_name") if isinstance(v.symbology, dict) else None)
                if v_style == style_name or f"style_{v.table_name}" == style_name or f"style_{v.layer_name}" == style_name:
                    if v.api_key_id == caller.id:
                        owned_match = True
                    else:
                        unowned_match = True

            if unowned_match:
                raise HTTPException(
                    status_code=403,
                    detail="Anda tidak memiliki izin menghapus style milik layer pengguna lain."
                )
            if not owned_match:
                raise HTTPException(
                    status_code=403,
                    detail="Hanya admin (PRIMARY) atau pemilik layer yang dapat menghapus style ini."
                )

        success = geoserver_client.delete_style(style_name=style_name, workspace_name=workspace, recurse=recurse)
        if not success:
            raise HTTPException(status_code=502, detail=f"GeoServer gagal menghapus style '{style_name}'.")

        return {"success": True, "detail": f"Style '{style_name}' berhasil dihapus."}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(f"Unexpected error deleting style '{style_name}': {exc}")
        raise HTTPException(status_code=500, detail="Terjadi kesalahan internal pada server.") from exc

