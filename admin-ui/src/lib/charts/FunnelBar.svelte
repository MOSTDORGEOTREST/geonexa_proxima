<script lang="ts">
	/** Воронка сбора: ординальная шкала одного тона, монотонная светлота. */
	import { funnelColor } from './palette';
	import { n } from './format';

	let { steps = [] }: { steps?: { label: string; value: number }[] } = $props();

	const max = $derived(Math.max(1, ...steps.map((step) => step.value)));
</script>

<div class="funnel">
	{#each steps as step, index}
		<div class="step">
			<span class="label">{step.label}</span>
			<div class="track">
				<div
					class="fill"
					style="width: {(step.value / max) * 100}%; background: {funnelColor(
						index,
						steps.length
					)}"
				></div>
			</div>
			<!-- Число всегда подписано: янтарь на бумаге даёт контраст 2.03,
			     и полагаться на один цвет здесь нельзя. -->
			<span class="value mono">{n(step.value)}</span>
			<span class="share muted">
				{steps[0]?.value ? Math.round((step.value / steps[0].value) * 100) : 0}%
			</span>
		</div>
	{/each}
</div>

<style>
	.funnel {
		display: grid;
		gap: 8px;
	}

	.step {
		display: grid;
		grid-template-columns: 120px 1fr 72px 48px;
		align-items: center;
		gap: 10px;
	}

	.label {
		font-size: 13px;
		color: var(--text-dim);
	}

	.track {
		background: var(--surface-2);
		border-radius: 4px;
		height: 14px;
		overflow: hidden;
	}

	.fill {
		height: 100%;
		border-radius: 0 4px 4px 0;
		min-width: 2px;
		transition: width var(--dur) var(--ease);
	}

	.value {
		text-align: right;
		font-variant-numeric: tabular-nums;
	}

	.share {
		text-align: right;
		font-size: 12px;
	}

	@media (max-width: 720px) {
		.step {
			grid-template-columns: 96px 1fr 60px;
		}

		.share {
			display: none;
		}
	}
</style>
