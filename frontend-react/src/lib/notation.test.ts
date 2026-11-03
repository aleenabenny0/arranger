import { describe, expect, it } from "vitest";
import { clampZoom, cssText, layoutOptions, markFindings, sanitise } from "./notation";

const SVG = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" id="s1" onload="alert(1)">
<style type="text/css"><![CDATA[#s1 .note { fill: red; }]]></style>
<script>alert(2)</script>
<foreignObject><div>x</div></foreignObject>
<g class="note" id="n1" style="fill:blue"><a href="https://evil.example/">x</a><use xlink:href="#glyph"/></g>
</svg>`;

describe("sanitise", () => {
  it("strips scripts, foreign objects, event handlers, inline styles and external links", () => {
    const canvas = document.createElement("div");
    document.body.append(canvas);
    const svg = sanitise(SVG, canvas);
    expect(svg.querySelector("script")).toBeNull();
    expect(svg.querySelector("foreignObject")).toBeNull();
    expect(svg.querySelector("style")).toBeNull();
    expect(svg.getAttribute("onload")).toBeNull();
    expect(svg.querySelector("#n1")?.getAttribute("style")).toBeNull();
    expect(svg.querySelector("a")?.getAttribute("href")).toBeNull();
    expect(svg.querySelector("use")?.getAttribute("xlink:href")).toBe("#glyph");
  });

  it("refuses text that is not SVG", () => {
    expect(() => sanitise("<svg><g></svg", document.createElement("div"))).toThrow(/could not be drawn/);
  });

  it("unwraps CDATA and entities in the lifted CSS", () => {
    expect(cssText("<![CDATA[a &lt; b &amp; c]]>")).toBe("a < b & c");
  });
});

describe("markFindings", () => {
  it("adds a ring with the severity and message to each marked note", () => {
    const canvas = document.createElement("div");
    canvas.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg"><g id="n1"></g><g id="n2"></g></svg>';
    for (const g of canvas.querySelectorAll("g")) (g as unknown as { getBBox: () => DOMRect }).getBBox = () => ({ x: 10, y: 20, width: 4, height: 8 } as DOMRect);
    markFindings(canvas, [{ severity: "hard", message: "Reach of 15 semitones.", note_ids: ["n1", "missing"] }]);
    markFindings(canvas, [{ severity: "hard", message: "again", note_ids: ["n1"] }]);   // no duplicate ring
    const rings = canvas.querySelectorAll(".finding-ring");
    expect(rings).toHaveLength(1);
    expect(rings[0].getAttribute("class")).toBe("finding-ring finding-hard");
    expect(rings[0].querySelector("title")?.textContent).toBe("Reach of 15 semitones.");
  });
});

describe("layout", () => {
  it("scales the page to the zoom and clamps the zoom", () => {
    expect(layoutOptions(800, 40)).toMatchObject({ pageWidth: 2000, scale: 40, breaks: "auto" });
    expect(clampZoom(10)).toBe(24);
    expect(clampZoom(100)).toBe(72);
    expect(clampZoom(48)).toBe(48);
  });
});
