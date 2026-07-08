/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Component, onWillStart, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

/**
 * P-UX bulk-tag dialog — pick existing tags and/or create a new one, then
 * apply them to the selected threads. Opened via ``dialog.add`` (which
 * auto-injects ``props.close``). On confirm, calls ``props.onConfirm(tagIds)``
 * with the full list of tag ids to append to every selected thread.
 */
export class LLMBulkTagDialog extends Component {
  static template = "llm_thread.LLMBulkTagDialog";
  static props = {
    threadIds: { type: Array, optional: true },
    onConfirm: { type: Function, optional: true },
    close: { type: Function, optional: true },
  };

  setup() {
    this.orm = useService("orm");
    this.notification = useService("notification");
    this.state = useState({
      tags: [], // [{id, name, color}]
      selected: {}, // {tagId: true}
      newTagName: "",
      creating: false,
    });

    onWillStart(() => this._loadTags());
  }

  async _loadTags() {
    try {
      const tags = await this.orm.searchRead(
        "llm.thread.tag",
        [],
        ["id", "name", "color"],
        { order: "name" }
      );
      this.state.tags = tags;
    } catch (e) {
      console.warn("[LLMBulkTagDialog] could not load tags:", e);
      this.notification.add(_t("Could not load tags."), { type: "danger" });
    }
  }

  toggleTag(tagId) {
    if (this.state.selected[tagId]) {
      const next = { ...this.state.selected };
      delete next[tagId];
      this.state.selected = next;
    } else {
      this.state.selected = { ...this.state.selected, [tagId]: true };
    }
  }

  get selectedIds() {
    return Object.keys(this.state.selected).map(Number);
  }

  async _createNewTag() {
    const name = (this.state.newTagName || "").trim();
    if (!name) {
      return null;
    }
    this.state.creating = true;
    try {
      // name_create dedups case-insensitively (llm.thread.tag override).
      const [tagId] = await this.orm.call("llm.thread.tag", "name_create", [
        name,
      ]);
      const tag = { id: tagId, name, color: 0 };
      this.state.tags = [...this.state.tags, tag];
      this.state.selected = { ...this.state.selected, [tagId]: true };
      this.state.newTagName = "";
      return tagId;
    } catch (e) {
      console.warn("[LLMBulkTagDialog] could not create tag:", e);
      this.notification.add(_t("Could not create the tag."), {
        type: "danger",
      });
      return null;
    } finally {
      this.state.creating = false;
    }
  }

  async onConfirm() {
    let ids = this.selectedIds;
    const createdId = await this._createNewTag();
    if (createdId) {
      ids = [...ids, createdId];
    }
    if (!ids.length) {
      this.notification.add(_t("Pick or create at least one tag."), {
        type: "warning",
      });
      return;
    }
    await this.props.onConfirm?.(ids);
    this.props.close?.();
  }

  onCancel() {
    this.props.close?.();
  }
}
