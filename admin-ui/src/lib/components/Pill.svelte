<script lang="ts">
	/**
	 * Ярлык состояния: цвет плюс текст.
	 *
	 * Только цветом состояние не кодируется никогда — восемь процентов мужчин
	 * не различают красный и зелёный, а печать и forced-colors не различают
	 * ничего.
	 */
	let { status, map = {} }: { status: string; map?: Record<string, string> } = $props();

	const TONE: Record<string, string> = {
		active: 'pill-good',
		sent: 'pill-good',
		succeeded: 'pill-good',
		completed: 'pill-good',
		ok: 'pill-good',
		trial: 'pill-good',
		creator: 'pill-good',
		administrator: 'pill-good',
		member: 'pill-good',
		running: 'pill-warn',
		queued: 'pill-warn',
		claimed: 'pill-warn',
		sending: 'pill-warn',
		pending: 'pill-warn',
		paused: 'pill-warn',
		restricted: 'pill-warn',
		borderline: 'pill-warn',
		failed: 'pill-bad',
		crashed: 'pill-bad',
		blocked: 'pill-bad',
		kicked: 'pill-bad',
		expired: 'pill-bad',
		rejected: 'pill-bad',
		cancelled: 'pill-mute',
		cancelling: 'pill-mute',
		scheduled: 'pill-mute',
		left: 'pill-mute',
		skipped: 'pill-mute'
	};
</script>

<span class="pill {TONE[status] ?? 'pill-mute'}">{map[status] ?? status}</span>
