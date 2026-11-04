def extract_text(message):
    """Finds and returns the text block among a Claude response's content blocks (with
    extended thinking, content[0] may be a ThinkingBlock, so it is located by type rather
    than by a fixed index)."""
    for block in message.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("The response contains no text block.")
