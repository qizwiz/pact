id: openai-chat
provider: OpenAI
api: openai.chat.completions.create
sdk_evidence:
  - "openai.types.chat.ChatCompletion.choices: list[Choice]"
  - "openai.types.chat.ChatCompletionMessage.content: Optional[str]"

contracts:
  - id: choices-nonempty
    before_accessing: response.choices[0]
    assert: len(response.choices) > 0
    rationale: >
      The API returns an empty choices list on error, content filtering,
      or when max_tokens is exceeded before any content is generated.

  - id: message-not-none
    before_accessing: response.choices[0].message
    assert: response.choices[0].message is not None
    rationale: >
      message can be None in streaming edge cases.

  - id: content-not-none
    before_accessing: response.choices[0].message.content
    assert: response.choices[0].message.content is not None
    rationale: >
      content is None when the response consists entirely of tool calls
      (finish_reason="tool_calls") or when the provider filters the output.
      This is the most common production crash site.

safe_pattern: |
  if (
      not response.choices
      or response.choices[0].message is None
      or response.choices[0].message.content is None
  ):
      raise ValueError("LLM returned empty or filtered response")
  return response.choices[0].message.content

source: https://platform.openai.com/docs/api-reference/chat/object
