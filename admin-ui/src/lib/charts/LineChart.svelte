<script lang="ts">
	/**
	 * Линейный график на голом SVG.
	 *
	 * Чарт-библиотека притащила бы свою палитру и своё представление о
	 * легендах, а дизайн-код задаёт и то, и другое. Слои здесь ровно те, что
	 * нужны: сетка, оси, марки, ховер.
	 */
	import { slotColor, type ColorSlot } from './palette';
	import { day, n } from './format';

	type Series = { key: string; label: string; color_slot?: ColorSlot };
	type Point = Record<string, unknown>;

	let {
		points = [],
		series = [],
		x = 'day',
		height = 220,
		area = false
	}: {
		points?: Point[];
		series?: Series[];
		x?: string;
		height?: number;
		area?: boolean;
	} = $props();

	const pad = { top: 12, right: 12, bottom: 26, left: 44 };
	const width = 720;

	let hover = $state<number | null>(null);

	const values = $derived(
		points.flatMap((point) => series.map((s) => Number(point[s.key] ?? 0)))
	);
	const max = $derived(Math.max(1, ...values));
	const innerWidth = width - pad.left - pad.right;
	const innerHeight = $derived(height - pad.top - pad.bottom);

	const px = (index: number): number =>
		pad.left + (points.length < 2 ? innerWidth / 2 : (index / (points.length - 1)) * innerWidth);
	const py = (value: number): number => pad.top + innerHeight - (value / max) * innerHeight;

	function path(key: string): string {
		return points
			.map((point, index) => `${index ? 'L' : 'M'}${px(index)},${py(Number(point[key] ?? 0))}`)
			.join(' ');
	}

	function areaPath(key: string): string {
		if (!points.length) return '';
		return `${path(key)} L${px(points.length - 1)},${pad.top + innerHeight} L${px(0)},${pad.top + innerHeight} Z`;
	}

	const ticks = $derived([0, 0.5, 1].map((share) => Math.round(max * share)));
</script>

{#if points.length}
	<div class="chart">
		<svg viewBox="0 0 {width} {height}" role="img" aria-label="График по дням">
			{#each ticks as tick}
				<line
					x1={pad.left}
					x2={width - pad.right}
					y1={py(tick)}
					y2={py(tick)}
					stroke="var(--viz-grid)"
					stroke-width="1"
				/>
				<text x={pad.left - 8} y={py(tick) + 4} text-anchor="end" class="axis">{n(tick)}</text>
			{/each}

			{#each series as s, index}
				{#if area}
					<path
						d={areaPath(s.key)}
						fill={slotColor(s.color_slot, index)}
						opacity="0.14"
						stroke="none"
					/>
				{/if}
				<path
					d={path(s.key)}
					fill="none"
					stroke={slotColor(s.color_slot, index)}
					stroke-width="2"
					stroke-linejoin="round"
					stroke-linecap="round"
				/>
			{/each}

			{#if hover !== null && points[hover]}
				<line
					x1={px(hover)}
					x2={px(hover)}
					y1={pad.top}
					y2={pad.top + innerHeight}
					stroke="var(--viz-axis)"
					stroke-width="1"
				/>
				{#each series as s, index}
					<circle
						cx={px(hover)}
						cy={py(Number(points[hover][s.key] ?? 0))}
						r="4"
						fill={slotColor(s.color_slot, index)}
						stroke="var(--viz-surface)"
						stroke-width="2"
					/>
				{/each}
			{/if}

			{#each points as point, index}
				{#if index % Math.ceil(points.length / 8) === 0}
					<text x={px(index)} y={height - 8} text-anchor="middle" class="axis">
						{day(point[x])}
					</text>
				{/if}
			{/each}

			<!-- Зона наведения шире марки: попасть в двухпиксельную линию мышью нельзя. -->
			{#each points as _, index}
				<rect
					x={px(index) - innerWidth / Math.max(1, points.length) / 2}
					y={pad.top}
					width={innerWidth / Math.max(1, points.length)}
					height={innerHeight}
					fill="transparent"
					onmouseenter={() => (hover = index)}
					onmouseleave={() => (hover = null)}
					role="presentation"
				/>
			{/each}
		</svg>

		<div class="legend">
			{#each series as s, index}
				<span class="item">
					<i style="background: {slotColor(s.color_slot, index)}"></i>
					{s.label}
					{#if hover !== null && points[hover]}
						<b>{n(points[hover][s.key])}</b>
					{/if}
				</span>
			{/each}
			{#if hover !== null && points[hover]}
				<span class="muted">{day(points[hover][x])}</span>
			{/if}
		</div>
	</div>
{:else}
	<p class="empty">Данных за период нет.</p>
{/if}

<style>
	.chart {
		display: grid;
		gap: 8px;
	}

	svg {
		width: 100%;
		height: auto;
		background: var(--viz-surface);
		border-radius: 12px;
	}

	.axis {
		fill: var(--muted);
		font-size: 11px;
		font-family: var(--font-m);
	}

	.legend {
		display: flex;
		flex-wrap: wrap;
		gap: 14px;
		font-size: 12.5px;
		color: var(--text-dim);
	}

	.item {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}

	.item i {
		width: 10px;
		height: 3px;
		border-radius: 2px;
	}

	.item b {
		font-family: var(--font-m);
		color: var(--text);
	}
</style>
