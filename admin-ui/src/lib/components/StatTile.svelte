<script lang="ts">
	import { n } from '$lib/charts/format';

	let {
		label,
		value,
		hint = '',
		tone = 'neutral'
	}: {
		label: string;
		value: unknown;
		hint?: string;
		tone?: 'neutral' | 'good' | 'warn' | 'bad';
	} = $props();
</script>

<div class="tile">
	<span class="label">{label}</span>
	<span class="value" class:good={tone === 'good'} class:warn={tone === 'warn'} class:bad={tone === 'bad'}>
		{typeof value === 'string' ? value : n(value)}
	</span>
	{#if hint}<span class="hint">{hint}</span>{/if}
</div>

<style>
	.good {
		color: var(--good);
	}

	.warn {
		color: var(--warning);
	}

	.bad {
		color: var(--critical);
	}
</style>
