"""Bounded visible-browser lookup for discovering the real result schema."""

import asyncio
import csv
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright

from .captcha import recognize


async def probe():
    with Path("roll_numbers_only.csv").open(encoding="utf-8-sig", newline="") as stream:
        roll = next(csv.DictReader(stream))["Roll Numbers"].strip()
    stamp = datetime.now(ZoneInfo("Asia/Karachi")).strftime("%Y%m%dT%H%M%S%f")
    root = Path("artifacts/probe") / stamp
    root.mkdir(parents=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 1000})
            page = await context.new_page()
            page.set_default_timeout(20000)
            for attempt in range(1, 4):
                folder = root / str(attempt)
                folder.mkdir()
                response = await page.goto("http://result.biselahore.com/", wait_until="domcontentloaded", timeout=60000)
                if response is None or not response.ok:
                    raise RuntimeError("Search page unavailable")
                await page.locator('label[for="rdlistCourse_0"]').click()
                await page.locator("#ddlExamType").select_option("2")
                await page.locator("#ddlExamYear").select_option("2026")
                await page.locator("#txtFormNo").fill(roll)
                await page.wait_for_function("() => { const i=document.querySelector('#imgCaptcha'); return i && i.complete && i.naturalWidth>0; }")
                png = await page.locator("#imgCaptcha").screenshot(path=str(folder / "captcha.png"))
                ocr = await asyncio.to_thread(recognize, png)
                (folder / "ocr.json").write_text(json.dumps(ocr, indent=2))
                if not ocr["text"]:
                    print(json.dumps({"attempt": attempt, "outcome": "ocr_unreadable"}), flush=True)
                    continue
                await page.locator("#txtCaptcha").fill(ocr["text"])
                async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                    await page.locator("#Button1").click()
                pages = []
                for index, current in enumerate(context.pages):
                    await current.wait_for_load_state("domcontentloaded")
                    await current.screenshot(path=str(folder / f"page-{index}.png"), full_page=True)
                    (folder / f"page-{index}.html").write_text(await current.content(), encoding="utf-8")
                    (folder / f"page-{index}.txt").write_text(await current.locator("body").inner_text(), encoding="utf-8")
                    structure = await current.evaluate("""() => ({
                      ids: [...document.querySelectorAll('[id]')].map(el=>({tag:el.tagName,id:el.id})),
                      tables: [...document.querySelectorAll('table')].map(el=>({id:el.id,rows:el.rows.length})),
                      errors: ['lblError','lblError2'].map(id=>({id,text:document.getElementById(id)?.innerText || ''}))
                    })""")
                    pages.append({"url": current.url, **structure})
                (folder / "structure.json").write_text(json.dumps(pages, indent=2))
                errors = " ".join(item["text"] for p in pages for item in p["errors"]).strip()
                print(json.dumps({"attempt": attempt, "pages": len(pages), "errors": errors, "evidence": str(folder)}), flush=True)
                if "captcha" not in errors.lower():
                    break
                await asyncio.sleep(3)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(probe())
