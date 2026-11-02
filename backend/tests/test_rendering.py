from io import BytesIO

import pytest
from PIL import Image
from pydantic import ValidationError

from app.agents.mock_model import mock_output
from app.agents.tools.image_renderer.html_playwright import CardRenderer
from app.schemas.content import Review, TaskCreate
from app.services.storage import Storage
from app.services.wechat_format import format_wechat


@pytest.mark.parametrize("theme", ["clean", "dark"])
async def test_real_browser_generates_unclipped_cards(tmp_path, theme):
    pack = mock_output("CardPack", {"task_input": TaskCreate(card_count=5).model_dump()})
    storage = Storage(tmp_path)
    files = await CardRenderer(storage).render("test-task", 1, pack, theme, "mock")
    assert len(files) == 5
    for file in files:
        image = Image.open(BytesIO(storage.resolve(file["path"]).read_bytes()))
        assert image.size == (1242, 1656)


def test_wechat_escapes_untrusted_markup_and_storage_path(tmp_path):
    html = format_wechat(
        {
            "title": "<script>alert(1)</script>",
            "markdown": "<script>alert(2)</script>\n\n[bad](javascript:alert(3))",
        }
    )
    assert "<script>" not in html and 'href="javascript:' not in html
    with pytest.raises(ValueError):
        Storage(tmp_path).resolve("../secret.env")


def test_rejection_requires_feedback():
    with pytest.raises(ValidationError):
        Review(interrupt_id="test", article_version=1, decision="rejected", operator="运营")
