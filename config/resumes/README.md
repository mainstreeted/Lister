# Resumes

Drop your two resume versions here as Markdown:

- `granular.md` — the long, detailed version with every project, bullet, metric.
  Used as the **source of truth** the tailorer pulls from when generating
  per-job variants.
- `formal.md` — the polished, concise version. Used as the visual/structural
  template for the final submitted PDF.

Both are gitignored. They never leave your machine except as inputs to the
Anthropic API at tailoring time.

## Why Markdown?

- Easy to edit.
- Easy for the LLM to remix.
- Easy to convert to PDF at submission time (via pandoc or weasyprint).

## Converting from PDF

If your current resumes are PDFs, the quickest path is to paste the text into
new `.md` files and add minimal structure. Suggested headings:

```markdown
# Your Name
contact line — email · phone · city · linkedin · github

## Summary
One-paragraph elevator pitch.

## Experience

### Job Title — Company  (YYYY-MM – YYYY-MM)
- Bullet with metric.
- Bullet with metric.

## Education

### Degree — School  (YYYY)

## Skills
Comma-separated or grouped.
```
