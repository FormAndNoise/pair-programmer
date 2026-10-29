import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";

app.registerExtension({
    name: "Comfy.SequentialMarkdownPromptLoader",
    async setup() {
        // FIX #2: Track has_more per node_id instead of a single shared boolean.
        // A single boolean causes a race when two Sequential Loader nodes exist
        // in the same workflow — the last event overwrites the first.
        const queueFlags = {};

        // Listen for status updates from the Python node.
        api.addEventListener("sequential_prompt_status", (event) => {
            const { node_id, has_more, current_index, total } = event.detail;
            queueFlags[node_id] = has_more;
            console.log(
                `[SequentialPrompt] Node ${node_id} — ` +
                `step ${current_index + 1}/${total}. ` +
                `Queue next: ${has_more}`
            );
        });

        // Listen for ComfyUI's status event, which fires when a generation finishes.
        api.addEventListener("status", (event) => {
            const status = event.detail;
            // exec_info.queue_remaining === 0 means the current workflow run is
            // completely done and the queue is empty.
            if (status && status.exec_info && status.exec_info.queue_remaining === 0) {
                const anyMore = Object.values(queueFlags).some(Boolean);
                if (anyMore) {
                    // Consume all flags immediately so only one re-queue fires
                    // even if multiple nodes reported has_more = true.
                    Object.keys(queueFlags).forEach((k) => { queueFlags[k] = false; });

                    // FIX #10: Brief delay lets ComfyUI finish internal state teardown
                    // (graph cleanup, widget updates) before we submit the next prompt.
                    setTimeout(() => {
                        app.queuePrompt(0, 1);
                    }, 150);
                }
            }
        });
    }
});
