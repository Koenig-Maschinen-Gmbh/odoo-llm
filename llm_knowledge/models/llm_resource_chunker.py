import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# Define default values as constants
DEFAULT_CHUNK_SIZE = 200
DEFAULT_CHUNK_OVERLAP = 20

# Opt-in Anthropic-style Contextual Retrieval (off by default). When enabled and a
# context model is configured, the structured chunker prepends a short
# LLM-generated context to each chunk before embedding (~35-67% fewer retrieval
# failures, at one LLM call per chunk at index time — validate cost/quality before
# enabling in production).
CONTEXTUAL_ENABLED_PARAM = "llm_knowledge.contextual_retrieval"
CONTEXTUAL_MODEL_PARAM = "llm_knowledge.contextual_model_id"
_CONTEXTUAL_PROMPT = (
    "<document>\n{doc}\n</document>\n\nHere is a chunk from the document:\n"
    "<chunk>\n{chunk}\n</chunk>\n\nGive a short (50-100 token) context that situates "
    "this chunk within the overall document, to improve search retrieval. Answer "
    "ONLY with the context, no preamble."
)


class LLMKnowledgeChunker(models.Model):
    _inherit = "llm.resource"

    # Chunking configuration fields
    chunker = fields.Selection(
        selection="_get_available_chunkers",
        string="Chunker",
        default="default",
        required=True,
        help="Method used to chunk resource content",
        tracking=True,
    )
    target_chunk_size = fields.Integer(
        string="Target Chunk Size",
        default=200,
        required=True,
        help="Target size of chunks in tokens",
        tracking=True,
    )
    target_chunk_overlap = fields.Integer(
        string="Chunk Overlap",
        default=20,
        required=True,
        help="Number of tokens to overlap between chunks",
        tracking=True,
    )

    chunk_ids = fields.One2many(
        "llm.knowledge.chunk",
        "resource_id",
        string="Chunks",
    )
    chunk_count = fields.Integer(
        string="Chunk Count",
        compute="_compute_chunk_count",
        store=True,
    )

    @api.model
    def _get_available_chunkers(self):
        """Get all available chunker methods"""
        return [
            ("default", "Default Chunker"),
            ("structured", "Structured (token-aware, Markdown-aware)"),
        ]

    @api.depends("chunk_ids")
    def _compute_chunk_count(self):
        for record in self:
            record.chunk_count = len(record.chunk_ids)

    def action_view_chunks(self):
        """Open a view with all chunks for this resource"""
        self.ensure_one()
        return {
            "name": _("Resource Chunks"),
            "view_mode": "list,form",
            "res_model": "llm.knowledge.chunk",
            "domain": [("resource_id", "=", self.id)],
            "type": "ir.actions.act_window",
            "context": {"default_resource_id": self.id},
        }

    def chunk(self):
        """Split the document into chunks"""
        for resource in self:
            if resource.state != "parsed":
                _logger.warning(
                    "Resource %s must be in parsed state to create chunks", resource.id
                )
                continue

        # Lock resources and process only the successfully locked ones
        resources = self._lock()
        if not resources:
            return False

        try:
            # Process each resource
            for resource in resources:
                try:
                    # Use appropriate chunker based on selection
                    success = False
                    if resource.chunker == "default":
                        success = resource._chunk_default()
                    elif resource.chunker == "structured":
                        success = resource._chunk_structured()
                    else:
                        _logger.warning(
                            "Unknown chunker %s, falling back to default",
                            resource.chunker,
                        )
                        success = resource._chunk_default()

                    if success:
                        # Mark as chunked
                        resource.write({"state": "chunked"})
                    else:
                        resource._post_styled_message(
                            "Failed to create chunks - no content or empty result",
                            "warning",
                        )

                except Exception as e:
                    resource._post_styled_message(
                        f"Error chunking resource: {str(e)}", "error"
                    )
                    resource._unlock()

            # Unlock all successfully processed resources
            resources._unlock()
            return True

        except Exception as e:
            resources._unlock()
            raise UserError(_("Error in batch chunking: %s") % str(e)) from e

    def _chunk_default(self):
        """
        Default implementation for splitting document into chunks.
        Uses a simple sentence-based splitting approach.
        """
        self.ensure_one()

        if not self.content:
            raise UserError(_("No content to chunk"))

        # Delete existing chunks
        self.chunk_ids.unlink()

        # Get chunking parameters
        chunk_size = self.target_chunk_size
        chunk_overlap = min(
            self.target_chunk_overlap, chunk_size // 2
        )  # Ensure overlap is not too large

        # Split content into sentences (simple regex-based approach)
        # Note: for a more sophisticated approach, consider using a NLP library
        sentences = re.split(r"(?<=[.!?])\s+", self.content)

        # Function to estimate token count (approximation)
        def estimate_tokens(text):
            # Simple approximation: 1 token ≈ 4 characters for English text
            return len(text) // 4

        # Create chunks using a sliding window approach
        chunks = []
        current_chunk = []
        current_size = 0

        for _i, sentence in enumerate(sentences):
            sentence_tokens = estimate_tokens(sentence)

            # If a single sentence exceeds chunk size, we have to include it anyway
            if current_size + sentence_tokens > chunk_size and current_chunk:
                # Create a chunk from accumulated sentences
                chunk_text = " ".join(current_chunk)
                chunk_seq = len(chunks) + 1

                # Create chunk record
                chunk = self.env["llm.knowledge.chunk"].create(
                    {
                        "resource_id": self.id,
                        "sequence": chunk_seq,
                        "content": chunk_text,
                        # Note: No need to set collection_ids as it's a related field
                    }
                )
                chunks.append(chunk)

                # Handle overlap: keep some sentences for the next chunk
                overlap_tokens = 0
                overlap_sentences = []

                # Work backwards through current_chunk to build overlap
                for sent in reversed(current_chunk):
                    sent_tokens = estimate_tokens(sent)
                    if overlap_tokens + sent_tokens <= chunk_overlap:
                        overlap_sentences.insert(0, sent)
                        overlap_tokens += sent_tokens
                    else:
                        break

                # Start new chunk with overlap sentences
                current_chunk = overlap_sentences
                current_size = overlap_tokens

            # Add current sentence to the chunk
            current_chunk.append(sentence)
            current_size += sentence_tokens

        # Don't forget the last chunk if there's anything left
        if current_chunk:
            chunk_text = " ".join(current_chunk)
            chunk_seq = len(chunks) + 1

            # Create chunk record
            chunk = self.env["llm.knowledge.chunk"].create(
                {
                    "resource_id": self.id,
                    "sequence": chunk_seq,
                    "content": chunk_text,
                    # Note: No need to set collection_ids as it's a related field
                }
            )
            chunks.append(chunk)

        # Post success message
        self._post_styled_message(
            f"Created {len(chunks)} chunks (target size: {chunk_size}, overlap: {chunk_overlap})",
            "success",
        )

        return len(chunks) > 0

    # ------------------------------------------------------------------
    # Structured, token-aware, Markdown-aware chunker (2026 baseline)
    # ------------------------------------------------------------------
    @staticmethod
    def _estimate_tokens_struct(text):
        """Token-count estimate without tiktoken.

        Counts word and punctuation runs (a close, slightly-conservative proxy
        for BPE tokens) and floors with chars/4. Used only to size chunks, so an
        approximation is fine; it keeps chunks a touch smaller than the target
        rather than overshooting an embedding model's window.
        """
        if not text:
            return 0
        word_punct = len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))
        return max(word_punct, len(text) // 4)

    @staticmethod
    def _split_blocks_struct(content):
        """Split text into structural blocks, keeping Markdown structure intact.

        A block is a heading line, a paragraph, a (whole) list, a fenced code
        block, or a table — separated by blank lines. Headings are emitted as
        their own block so the packer can prefer to start a new chunk at a
        section boundary.
        """
        lines = content.replace("\r\n", "\n").split("\n")
        blocks = []
        buf = []
        in_fence = False

        def flush():
            if buf:
                text = "\n".join(buf).strip()
                if text:
                    blocks.append(text)
                buf.clear()

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                # Toggle fenced code; keep the whole fence in one block.
                buf.append(line)
                if in_fence:
                    flush()
                in_fence = not in_fence
                continue
            if in_fence:
                buf.append(line)
                continue
            if not stripped:
                flush()
                continue
            if stripped.startswith("#"):
                # Heading is its own block (section boundary).
                flush()
                blocks.append(stripped)
                continue
            buf.append(line)
        flush()
        return blocks

    @staticmethod
    def _heading_level_struct(block):
        """Markdown heading level (number of leading '#'), or 0 if not a heading."""
        m = re.match(r"^(#{1,6})\s", block)
        return len(m.group(1)) if m else 0

    @staticmethod
    def _update_heading_stack_struct(stack, block):
        """Maintain a (level, text) heading stack as blocks are consumed: a new
        heading pops same-or-deeper levels then pushes itself."""
        level = LLMKnowledgeChunker._heading_level_struct(block)
        if not level:
            return
        text = block.lstrip("#").strip()
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    @staticmethod
    def _breadcrumb_struct(stack):
        """Render the heading stack as a 'Title > Section > Subsection' crumb."""
        return " > ".join(text for _level, text in stack if text)

    @classmethod
    def _split_oversized_block_struct(cls, block, max_tokens):
        """Split a single block that exceeds max_tokens into sentence groups."""
        sentences = re.split(r"(?<=[.!?])\s+", block)
        out = []
        cur = []
        cur_tok = 0
        for sent in sentences:
            tok = cls._estimate_tokens_struct(sent)
            if cur and cur_tok + tok > max_tokens:
                out.append(" ".join(cur))
                cur = []
                cur_tok = 0
            cur.append(sent)
            cur_tok += tok
        if cur:
            out.append(" ".join(cur))
        return out

    @api.model
    def _contextual_model(self):
        """The configured contextual-retrieval chat model, or None when the
        feature is disabled / unconfigured. Opt-in (default OFF)."""
        icp = self.env["ir.config_parameter"].sudo()
        if icp.get_param(CONTEXTUAL_ENABLED_PARAM, default="0") not in (
            "1",
            "True",
            "true",
        ):
            return None
        param = icp.get_param(CONTEXTUAL_MODEL_PARAM)
        if not param:
            return None
        model = self.env["llm.model"].sudo().browse(int(param)).exists()
        return model or None

    def _contextualize_chunks(self, model, full_content, chunks_text):
        """Prepend an LLM-generated context blurb to each chunk (Anthropic
        Contextual Retrieval). Best-effort per chunk: any failure leaves that
        chunk unchanged, so indexing never breaks on a provider hiccup."""
        full = (full_content or "")[:8000]
        out = []
        for text in chunks_text:
            ctx = ""
            try:
                result = model.sudo().chat(
                    self.env["mail.message"].sudo(),
                    stream=False,
                    prepend_messages=[
                        {
                            "role": "user",
                            "content": _CONTEXTUAL_PROMPT.format(
                                doc=full, chunk=text[:1500]
                            ),
                        }
                    ],
                )
                ctx = (result.get("content") or "").strip().replace("\n", " ")[:400]
            except Exception:
                _logger.exception(
                    "llm_knowledge: contextual retrieval failed for a chunk; using chunk as-is"
                )
                ctx = ""
            out.append(f"Context: {ctx}\n\n{text}" if ctx else text)
        return out

    def _chunk_structured(self):
        """Token-aware, Markdown-aware recursive chunker (2026 baseline).

        Packs structural blocks (headings/paragraphs/lists/code/tables) greedily
        up to ``target_chunk_size`` tokens with ``target_chunk_overlap`` tokens of
        trailing-block overlap, never splitting a block unless it alone exceeds
        the target. Prefers to start a new chunk at a heading. Replaces the naive
        200-token, char-based sentence splitter for structure-rich content.
        """
        self.ensure_one()

        if not self.content:
            raise UserError(_("No content to chunk"))

        self.chunk_ids.unlink()

        target = max(self.target_chunk_size or 0, 64)
        overlap = max(min(self.target_chunk_overlap or 0, target // 2), 0)

        raw_blocks = self._split_blocks_struct(self.content)
        # Expand any single oversized block into smaller pieces up front.
        blocks = []
        for block in raw_blocks:
            if self._estimate_tokens_struct(block) > target:
                blocks.extend(self._split_oversized_block_struct(block, target))
            else:
                blocks.append(block)

        # Each chunk is prefixed with a "Context: Title > Section" breadcrumb
        # built from the Markdown heading hierarchy — a cheap, deterministic
        # contextual-retrieval signal (situates the chunk in its document) with
        # no per-chunk LLM call. Captured from the heading stack at the chunk's
        # first block (ancestor headings).
        # Optional Anthropic-style LLM-generated contextual retrieval (opt-in,
        # default OFF). When configured it replaces the deterministic breadcrumb
        # with a per-chunk LLM context blurb.
        llm_ctx_model = self._contextual_model()

        chunks_text = []
        heading_stack = []
        cur = []
        cur_tok = 0
        cur_crumb = ""

        def emit(crumb, packed):
            body = "\n\n".join(packed)
            if crumb and not llm_ctx_model:
                body = f"Context: {crumb}\n\n{body}"
            chunks_text.append(body)

        for block in blocks:
            tok = self._estimate_tokens_struct(block)
            is_heading = bool(self._heading_level_struct(block))
            # Emit the current chunk when adding this block would overflow, or
            # when we hit a heading and the chunk already has substance.
            if cur and (
                cur_tok + tok > target or (is_heading and cur_tok >= target // 2)
            ):
                emit(cur_crumb, cur)
                # Build overlap from trailing blocks of the just-emitted chunk.
                ov = []
                ov_tok = 0
                for prev in reversed(cur):
                    ptok = self._estimate_tokens_struct(prev)
                    if ov_tok + ptok > overlap:
                        break
                    ov.insert(0, prev)
                    ov_tok += ptok
                cur = list(ov)
                cur_tok = ov_tok
                cur_crumb = self._breadcrumb_struct(heading_stack)
            if not cur:
                cur_crumb = self._breadcrumb_struct(heading_stack)
            cur.append(block)
            cur_tok += tok
            if is_heading:
                self._update_heading_stack_struct(heading_stack, block)

        if cur:
            emit(cur_crumb, cur)

        if llm_ctx_model:
            chunks_text = self._contextualize_chunks(
                llm_ctx_model, self.content, chunks_text
            )

        for seq, text in enumerate(chunks_text, 1):
            self.env["llm.knowledge.chunk"].create(
                {
                    "resource_id": self.id,
                    "sequence": seq,
                    "content": text,
                }
            )

        self._post_styled_message(
            f"Created {len(chunks_text)} structured chunks "
            f"(target: {target} tok, overlap: {overlap} tok)",
            "success",
        )
        return len(chunks_text) > 0

    def action_reset_chunk_settings(self):
        """Reset chunk settings to system defaults"""

        # Reset all selected resources to default values
        self.write(
            {
                "target_chunk_size": DEFAULT_CHUNK_SIZE,
                "target_chunk_overlap": DEFAULT_CHUNK_OVERLAP,
                "chunker": "default",
            }
        )

        # Return action to reload the form view
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id if len(self) == 1 else False,
            "view_mode": "form" if len(self) == 1 else "list,form",
            "target": "current",
        }
