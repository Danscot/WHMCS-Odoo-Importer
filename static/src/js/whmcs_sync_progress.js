/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Component, onMounted, onWillUnmount, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

class WhmcsSyncProgress extends Component {
    static template = "whmcs_odoo_import.WhmcsSyncProgress";

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");
        this.state = useState({
            phase: "Starting synchronization…",
            done: false,
            failed: false,
        });
        this.timer = null;
        onMounted(() => this._poll());
        onWillUnmount(() => {
            if (this.timer) clearTimeout(this.timer);
        });
    }

    async _poll() {
        try {
            const rows = await this.orm.read(
                "whmcs.import.batch",
                [this.props.action.params.batch_id],
                ["state", "total_clients", "total_invoices", "total_transactions", "error_count", "warning_count"]
            );
            const batch = rows && rows[0];
            if (!batch) {
                this.state.failed = true;
                this.state.phase = "Synchronization batch not found.";
                return;
            }
            if (batch.state === "done" || batch.state === "done_with_warnings") {
                this.state.done = true;
                this.state.phase = batch.state === "done" ? "Synchronization completed." : "Synchronization completed with warnings.";
                this.notification.add(this.state.phase, {
                    type: batch.state === "done" ? "success" : "warning",
                    sticky: false,
                });
                this.action.doAction({
                    type: "ir.actions.act_window",
                    res_model: "whmcs.import.batch",
                    res_id: batch.id,
                    views: [[false, "form"]],
                    target: "current",
                });
                return;
            }
            if (batch.state === "failed") {
                this.state.failed = true;
                this.state.phase = "Synchronization failed. Check Import History for details.";
                this.notification.add(this.state.phase, { type: "danger", sticky: true });
                return;
            }
            this.state.phase = "Synchronizing with WHMCS…";
            this.timer = setTimeout(() => this._poll(), 1500);
        } catch (error) {
            this.state.failed = true;
            this.state.phase = "Unable to read synchronization status.";
        }
    }
}

registry.category("actions").add(
    "whmcs_sync_progress",
    WhmcsSyncProgress
);
