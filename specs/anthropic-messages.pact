id: anthropic-messages
provider: Anthropic
api: anthropic.messages.create
sdk_evidence:
  - "anthropic.types.Message.content: list[ContentBlock]"
  - "anthropic.types.TextBlock.text: str"

contracts:
  - id: content-nonempty
    before_accessing: response.content[0]
    assert: len(response.content) > 0
    rationale: >
      The API returns an empty content list when the response is filtered
      or when stop_reason is "end_turn" with no generated text.

  - id: text-not-none
    before_accessing: response.content[0].text
    assert: response.content[0].text is not None
    rationale: >
      A ContentBlock may be a ToolUseBlock (no .text attribute) or a
      TextBlock with text=None on filtered output. Accessing .text without
      checking block type or None raises AttributeError or returns None.

safe_pattern: |
  if not response.content or response.content[0].text is None:
      raise ValueError("LLM returned empty or filtered response")
  return response.content[0].text

source: https://docs.anthropic.com/en/api/messages
