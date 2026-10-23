"""Bounded chart-image fallback, parameter types and resumable paid work."""
from copy import deepcopy
from threading import Event

import pytest
from backend.materials.chart_crops import complete_chart_crops, same_region
from backend.materials.document_pipeline import normalize_result, paddle_document_result
from backend.materials.storage import MaterialStorage
from backend.slice1.adapters.paddle_vl import (
    DOCUMENT_OPTIONS,
    crop_document_options,
    document_options,
)
from backend.slice1.errors import AdapterError
from test_document_attachments import pdf
from test_paddle_crops import picture


def page(*, supplemental=False, shifted=False, angle=0):
    box = [100 + (30 if shifted else 0), 200, 800, 900]
    return {"prunedResult": {"width": 1000, "height": 1000,
        "doc_preprocessor_res": {"angle": angle, "model_settings": {"use_doc_unwarping": True}},
        "parsing_res_list": [
            {"block_label": "text", "block_content": "SECONDARY MUST NOT REPLACE OCR" if supplemental else "Main OCR evidence"},
            {"block_label": "chart", "block_content": "x | y\n1 | 2", "block_bbox": box}]},
        "markdown": {"images": {"imgs/img_in_chart_box_100_200_800_900.jpg":
            "https://pplines-online.bj.bcebos.com/synthetic-chart"} if supplemental else {}}}


class Adapter:
    def __init__(self, result=None):
        self.document_options = document_options()
        self.submissions = []
        self.result = result or paddle_document_result([page(supplemental=True)], requested_options=crop_document_options())

    def submit_document(self, payload, kind):
        self.submissions.append((deepcopy(self.document_options), kind, payload[:5]))
        return "synthetic-chart-job"

    def check_document(self, ident):
        return self.result

    def download_crop(self, url):
        return picture()


def run(tmp_path, *, adapter=None, batch=None, stop=None):
    path = tmp_path / "synthetic.pdf"
    path.write_bytes(pdf())
    normalized = normalize_result(paddle_document_result([page()], requested_options=DOCUMENT_OPTIONS))
    state = {"ocr_crop_options": crop_document_options()}
    batch = batch if batch is not None else {"job_id": "synthetic-primary-job"}
    adapter = adapter or Adapter()
    checkpoints = []
    result = complete_chart_crops(normalized, state, batch, source=path,
        storage=MaterialStorage(tmp_path / "storage"), material_id="synthetic-material", adapter=adapter,
        guard=lambda: None, checkpoint=lambda value: checkpoints.append(deepcopy(batch)), stop=stop or Event())
    return result, normalized, batch, adapter, checkpoints


def test_chart_crop_only_pass_preserves_primary_text_and_restores_all_primary_features(tmp_path):
    result, normalized, batch, adapter, checkpoints = run(tmp_path)
    assert result and normalized["page_results"][0]["blocks"][0]["content"] == "Main OCR evidence"
    figure = normalized["page_results"][0]["blocks"][1]
    assert figure["crop"]["width"] == 120 and figure["crop_provider_pass"] == "chart_image"
    assert batch["crop_status"] == "completed" and batch["crop_missing"] == 0
    assert batch["job_id"] == "synthetic-primary-job"
    assert adapter.document_options == DOCUMENT_OPTIONS and len(adapter.submissions) == 1
    options, kind, magic = adapter.submissions[0]
    assert options["useChartRecognition"] is False and DOCUMENT_OPTIONS["useChartRecognition"] is True
    assert all(options[name] is True for name in ("useDocUnwarping", "useDocOrientationClassify",
                                                "useOcrForImageBlock", "useSealRecognition")
               if name != "useDocUnwarping")
    assert options["useDocUnwarping"] is False and DOCUMENT_OPTIONS["useDocUnwarping"] is True
    assert kind == "pdf" and magic == b"%PDF-"
    assert checkpoints[0]["crop_job_id"] == "submitting"


def test_resume_reuses_chart_job_instead_of_submitting_again(tmp_path):
    batch = {"job_id": "synthetic-primary-job", "crop_pages": [1],
             "crop_job_id": "existing-chart-job", "crop_status": "processing"}
    _, _, saved, adapter, _ = run(tmp_path, batch=batch)
    assert not adapter.submissions and saved["crop_job_id"] == "existing-chart-job"


@pytest.mark.parametrize("shifted,angle", [(True, 0), (False, 90)])
def test_changed_geometry_or_frame_never_binds_the_wrong_chart_crop(tmp_path, shifted, angle):
    adapter = Adapter(paddle_document_result([page(supplemental=True, shifted=shifted, angle=angle)],
                                           requested_options=crop_document_options()))
    _, normalized, batch, _, _ = run(tmp_path, adapter=adapter)
    assert "crop" not in normalized["page_results"][0]["blocks"][1]
    assert batch["crop_missing"] == 1


def test_cancelled_worker_does_not_start_a_new_crop_job(tmp_path):
    stop = Event()
    stop.set()
    result, _, _, adapter, _ = run(tmp_path, stop=stop)
    assert result is False and not adapter.submissions


def test_ambiguous_or_failed_supplement_preserves_primary_job(tmp_path):
    batch = {"job_id": "synthetic-primary-job", "crop_job_id": "submitting"}
    with pytest.raises(AdapterError):
        run(tmp_path, batch=batch)
    assert batch["job_id"] == "synthetic-primary-job"
    adapter = Adapter({"status": "FAILED"})
    batch = {"job_id": "synthetic-primary-job"}
    with pytest.raises(AdapterError):
        run(tmp_path, batch=batch, adapter=adapter)
    assert batch["crop_status"] == "failed" and batch["job_id"] == "synthetic-primary-job"


@pytest.mark.parametrize("extra", [
    {"layoutThreshold": True}, {"layoutThreshold": -0.1}, {"layoutThreshold": 1.1},
    {"layoutThreshold": float("nan")}, {"layoutUnclipRatio": 0},
    {"layoutUnclipRatio": [1, 2, 3]}, {"layoutUnclipRatio": [True, 1]},
    {"layoutMergeBboxesMode": "guess"},
])
def test_layout_parameters_reject_invalid_values(extra):
    with pytest.raises(ValueError):
        document_options({**DOCUMENT_OPTIONS, **extra})


def test_parameter_snapshot_and_crop_profile_are_independent():
    options = document_options({**DOCUMENT_OPTIONS, "layoutUnclipRatio": [1.1, 1.05]})
    second = crop_document_options(options)
    second["layoutUnclipRatio"][0] = 2
    assert options["layoutUnclipRatio"] == [1.1, 1.05]
    assert options["useChartRecognition"] is True and second["useChartRecognition"] is False


def test_only_nearly_identical_boxes_can_bridge_detection_jitter():
    assert same_region([.1, .2, .8, .9], [.101, .2, .8, .901])
    assert not same_region([.1, .2, .8, .9], [.2, .2, .9, .9])
    assert not same_region([.1, .2, .11, .21], [.102, .202, .112, .212])


def test_unwarped_and_original_frames_keep_independent_images_even_when_labels_change(tmp_path):
    extra = page(supplemental=True)
    extra["prunedResult"]["doc_preprocessor_res"]["model_settings"]["use_doc_unwarping"] = False
    extra["prunedResult"]["parsing_res_list"].append({
        "block_label": "image", "block_content": "", "block_bbox": [700, 30, 900, 200]})
    extra["markdown"]["images"]["imgs/img_in_image_box_700_30_900_200.jpg"] = (
        "https://pplines-online.bj.bcebos.com/synthetic-reclassified-image")
    adapter = Adapter(paddle_document_result([extra], requested_options=crop_document_options()))
    _, normalized, batch, _, _ = run(tmp_path, adapter=adapter)
    blocks = normalized["page_results"][0]["blocks"]
    assert "crop" not in blocks[1]  # The primary processed frame is never rewritten.
    originals = [b for b in blocks if b.get("crop_provider_pass") == "chart_image_original"]
    assert len(originals) == 2 and all(b.get("crop") and b.get("source_bbox") for b in originals)
    assert {b["provider_label"] for b in originals} == {"image", "chart"}
    assert batch["standalone_figure_crops"] == 2 and batch["crop_missing"] == 1
