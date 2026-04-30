import { describe, expect, it } from "vitest";

import {
  renderMarkdownForSlack,
  splitMarkdownForSlackMessages,
} from "../src/lib/slack/markdown";

describe("Slack markdown rendering", () => {
  it("omits fenced code language labels from Slack mrkdwn", () => {
    const rendered = renderMarkdownForSlack([
      "```rust",
      "fn main() {",
      "    println!(\"Hello, world!\");",
      "}",
      "```",
    ].join("\n"));

    expect(rendered.text).toBe([
      "```",
      "fn main() {",
      "    println!(\"Hello, world!\");",
      "}",
      "```",
    ].join("\n"));
  });

  it("renders markdown tables as native Slack table blocks", () => {
    const rendered = renderMarkdownForSlack([
      "Summary",
      "",
      "| Asset | Value |",
      "| --- | --- |",
      "| BTC | $1.00M |",
    ].join("\n"));

    expect(rendered.blocks?.some((block) => block.type === "table")).toBe(true);
  });

  it("renders headings as separate Slack section blocks", () => {
    const rendered = renderMarkdownForSlack([
      "# Summary",
      "",
      "Short intro.",
      "",
      "## Details",
      "",
      "- One",
    ].join("\n"));

    expect(rendered.blocks).toEqual([
      { type: "section", text: { type: "mrkdwn", text: "*Summary*" } },
      { type: "section", text: { type: "mrkdwn", text: "Short intro." } },
      { type: "section", text: { type: "mrkdwn", text: "*Details*" } },
      { type: "section", text: { type: "mrkdwn", text: "- One" } },
    ]);
  });

  it("renders separate paragraphs as separate Slack section blocks", () => {
    const rendered = renderMarkdownForSlack([
      "First paragraph.",
      "",
      "Second paragraph.",
      "",
      "Third paragraph.",
    ].join("\n"));

    expect(rendered.blocks).toEqual([
      { type: "section", text: { type: "mrkdwn", text: "First paragraph." } },
      { type: "section", text: { type: "mrkdwn", text: "Second paragraph." } },
      { type: "section", text: { type: "mrkdwn", text: "Third paragraph." } },
    ]);
  });

  it("packs Slack markdown messages up to the block budget", () => {
    const markdown = Array.from({ length: 55 }, (_, i) => `Paragraph ${i}.`).join("\n\n");

    const chunks = splitMarkdownForSlackMessages(markdown, { firstMaxBlocks: 49 });

    expect(chunks).toHaveLength(2);
    expect(renderMarkdownForSlack(chunks[0]).blocks).toHaveLength(49);
    expect(renderMarkdownForSlack(chunks[1]).blocks).toHaveLength(6);
  });

  it("keeps plain text messages together up to Slack's 40k text limit", () => {
    const text = "a".repeat(39_999);

    expect(splitMarkdownForSlackMessages(text)).toEqual([text]);
  });
});
