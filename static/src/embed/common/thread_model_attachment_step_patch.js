import { Thread } from "@mail/core/common/thread_model";

import { patch } from "@web/core/utils/patch";

patch(Thread.prototype, {
    get composerDisabled() {
        const step = this.chatbot?.currentStep;
        if (
            this.channel_type === "livechat" &&
            step?.type === "question_attachment" &&
            !this.livechat_end_dt
        ) {
            if (this.chatbot?.forwarded) {
                return false;
            }
            return Boolean(
                this.chatbot?.isProcessingAnswer ||
                    (step && !step.operatorFound && step.completed)
            );
        }
        return super.composerDisabled;
    },
});
