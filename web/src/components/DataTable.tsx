// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Fragment, useState, useEffect, useMemo, type ReactNode } from 'react'
import {
  ColumnDef,
  ColumnFiltersState,
  Column,
  ColumnVisibilityState,
  ExpandedState,
  RowData,
  SortingState,
  RowSelectionState,
  columnFacetingFeature,
  columnFilteringFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  createFacetedRowModel,
  createFacetedUniqueValues,
  createFilteredRowModel,
  createPaginatedRowModel,
  createSortedRowModel,
  filterFn_equalsString,
  filterFn_includesString,
  filterFn_weakEquals,
  flexRender,
  metaHelper,
  rowExpandingFeature,
  rowPaginationFeature,
  rowSelectionFeature,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_text,
  tableFeatures,
  useTable,
} from '@tanstack/react-table'
import { ChevronUp, ChevronDown, ChevronsUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, X } from 'lucide-react'
import { cn } from '../utils/cn'

interface DataTableColumnMeta {
  filterVariant?: 'text' | 'select'
  /** Fixed choices for a select filter; without them the choices are the values present in the loaded rows. */
  filterOptions?: { value: string; label: string }[]
  /** Right-align numeric columns. */
  align?: 'left' | 'right'
}

// What every table here can do. 'auto' filtering and sorting only pick from the
// functions registered below: strings filter with includesString and sort with
// alphanumeric or text, and a column with no values filters with weakEquals.
// Numbers have no 'auto' filter: a filtered number column names one, as the
// status columns do with equalsString.
const features = tableFeatures({
  columnFilteringFeature,
  columnFacetingFeature,
  columnSizingFeature,
  columnVisibilityFeature,
  rowExpandingFeature,
  rowPaginationFeature,
  rowSelectionFeature,
  rowSortingFeature,
  filteredRowModel: createFilteredRowModel(),
  facetedRowModel: createFacetedRowModel(),
  facetedUniqueValues: createFacetedUniqueValues(),
  paginatedRowModel: createPaginatedRowModel(),
  sortedRowModel: createSortedRowModel(),
  filterFns: {
    includesString: filterFn_includesString,
    equalsString: filterFn_equalsString,
    weakEquals: filterFn_weakEquals,
  },
  sortFns: { alphanumeric: sortFn_alphanumeric, text: sortFn_text },
  columnMeta: metaHelper<DataTableColumnMeta>(),
})

export type DataTableFeatures = typeof features

/** Room a column with no explicit size is guaranteed before the table starts scrolling sideways. */
const UNSIZED_COLUMN_MIN_WIDTH = 170

/** Paging driven by the server: `data` is one page and `total` is the size of the whole result. */
export interface ManualPagination {
  pageIndex: number
  pageSize: number
  total: number
  /** False when the server stopped counting: `total` is then a lower bound, shown with a "+", and there is no last page to go to. */
  totalExact?: boolean
  /** Offered beside a lower-bound total: ask the server to count everything. */
  countAll?: { onCount: () => void; counting: boolean; error?: string }
  onPageChange: (pageIndex: number) => void
  onPageSizeChange: (pageSize: number) => void
}

/** Column filters owned by the caller, who applies them (typically on the server) instead of the table. */
export interface ManualFiltering {
  columnFilters: ColumnFiltersState
  onColumnFiltersChange: (filters: ColumnFiltersState) => void
}

interface DataTableProps<TData extends RowData> {
  columns: ColumnDef<DataTableFeatures, TData>[]
  data: TData[]
  /** When set, the table shows `data` as one page of `total` rows and asks for other pages instead of slicing. */
  manualPagination?: ManualPagination
  /** When set, the header filters report to the caller and the table does no filtering of its own. Implies enableColumnFilters. */
  manualFiltering?: ManualFiltering
  defaultPageSize?: number
  emptyMessage?: string
  enableRowSelection?: boolean
  enableColumnFilters?: boolean
  onSelectionChange?: (selectedRows: TData[]) => void
  getRowId?: (row: TData) => string
  renderExpandedRow?: (row: TData) => ReactNode
  /** Clicking a row (outside buttons/inputs) opens it, e.g. in the docked inspector. */
  onRowClick?: (row: TData) => void
  /** Row id (from getRowId) currently open in the inspector; gets a highlight. */
  activeRowId?: string | null
  /** Controlled column visibility, e.g. to drop columns while a panel is open. */
  columnVisibility?: ColumnVisibilityState
  /** Renders in the footer while rows are selected. */
  bulkActions?: (selected: TData[], clearSelection: () => void) => ReactNode
  className?: string
}

export function DataTable<TData extends RowData>({
  columns,
  data,
  manualPagination,
  manualFiltering,
  defaultPageSize = 20,
  emptyMessage = 'No data available.',
  enableRowSelection = false,
  enableColumnFilters = false,
  onSelectionChange,
  getRowId,
  renderExpandedRow,
  onRowClick,
  activeRowId,
  columnVisibility,
  bulkActions,
  className,
}: DataTableProps<TData>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  const [ownColumnFilters, setOwnColumnFilters] = useState<ColumnFiltersState>([])
  const [expanded, setExpanded] = useState<ExpandedState>({})
  const columnFilters = manualFiltering ? manualFiltering.columnFilters : ownColumnFilters
  const setColumnFilters = (updater: ColumnFiltersState | ((old: ColumnFiltersState) => ColumnFiltersState)) => {
    const next = typeof updater === 'function' ? updater(columnFilters) : updater
    if (manualFiltering) manualFiltering.onColumnFiltersChange(next)
    else setOwnColumnFilters(next)
  }
  const showColumnFilters = enableColumnFilters || !!manualFiltering

  const table = useTable({
    features,
    data,
    columns,
    getRowCanExpand: renderExpandedRow ? () => true : undefined,
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    onColumnFiltersChange: setColumnFilters,
    onExpandedChange: setExpanded,
    enableRowSelection,
    getRowId,
    autoResetPageIndex: false,
    // Refetches replace `data`; open detail rows stay open.
    autoResetExpanded: false,
    // A shift-click toggles one row, like any other click.
    enableRowRangeSelection: false,
    ...(manualFiltering ? { manualFiltering: true } : {}),
    ...(manualPagination
      ? {
          manualPagination: true,
          pageCount: Math.max(1, Math.ceil(manualPagination.total / manualPagination.pageSize)),
          onPaginationChange: (updater) => {
            const current = { pageIndex: manualPagination.pageIndex, pageSize: manualPagination.pageSize }
            const next = typeof updater === 'function' ? updater(current) : updater
            if (next.pageSize !== current.pageSize) manualPagination.onPageSizeChange(next.pageSize)
            else if (next.pageIndex !== current.pageIndex) manualPagination.onPageChange(next.pageIndex)
          },
        }
      : {}),
    state: {
      sorting,
      rowSelection,
      columnFilters,
      expanded,
      ...(columnVisibility ? { columnVisibility } : {}),
      ...(manualPagination ? { pagination: { pageIndex: manualPagination.pageIndex, pageSize: manualPagination.pageSize } } : {}),
    },
    initialState: {
      pagination: {
        pageIndex: 0,
        pageSize: defaultPageSize,
      },
    },
  })

  // If the current page becomes invalid after data changes, go to the last page.
  const pageCount = table.getPageCount()
  const currentPageIndex = table.state.pagination.pageIndex
  useEffect(() => {
    if (pageCount > 0 && currentPageIndex >= pageCount) {
      table.setPageIndex(pageCount - 1)
    }
  }, [pageCount, currentPageIndex, table])

  const selectedRows = table.getFilteredSelectedRowModel().rows.map((row) => row.original)
  useEffect(() => {
    onSelectionChange?.(selectedRows)
  }, [rowSelection, onSelectionChange])

  const pageSize = table.state.pagination.pageSize
  const totalRows = manualPagination ? manualPagination.total : table.getFilteredRowModel().rows.length
  const totalExact = manualPagination?.totalExact ?? true
  const countAll = manualPagination?.countAll
  const visibleColumns = table.getVisibleLeafColumns()
  const visibleColumnCount = visibleColumns.length
  // The table is fixed-layout and fills its container, so without a floor the
  // sized columns would eat all the room on a narrow screen and the unsized ones
  // (host, connector name) would collapse to nothing. Below this width the
  // wrapper scrolls sideways instead, with the identifying column still first.
  const minTableWidth = visibleColumns.reduce((sum, col) => sum + (col.getSize() !== 150 ? col.getSize() : UNSIZED_COLUMN_MIN_WIDTH), 0)

  const handleRowClick = (e: React.MouseEvent, original: TData) => {
    if (!onRowClick) return
    const target = e.target as HTMLElement
    if (target.closest('button, a, input, select, textarea, label, [data-no-row-click]')) return
    onRowClick(original)
  }

  return (
    <div className={cn('bg-surface rounded-lg border border-line overflow-hidden min-w-0 flex flex-col', className)}>
      <div className="overflow-x-auto @container">
        <table className="w-full text-[13px] table-fixed" style={{ minWidth: minTableWidth }}>
          <thead>
            <tr className="bg-surface-raised border-b border-line">
              {table.getHeaderGroups().map((headerGroup) =>
                headerGroup.headers.map((header) => {
                  const align = header.column.columnDef.meta?.align
                  return (
                    <th
                      key={header.id}
                      style={{ width: header.getSize() !== 150 ? header.getSize() : undefined }}
                      className={cn(
                        'px-3 py-2 text-left text-[11px] font-semibold text-fg-muted uppercase tracking-wider whitespace-nowrap overflow-hidden text-ellipsis',
                        header.column.getCanSort() && 'cursor-pointer select-none hover:text-fg',
                        align === 'right' && 'text-right'
                      )}
                      onClick={header.column.getToggleSortingHandler()}
                    >
                      <div className={cn('flex items-center gap-1 min-w-0', align === 'right' && 'justify-end')}>
                        {header.isPlaceholder
                          ? null
                          : flexRender(header.column.columnDef.header, header.getContext())}
                        {header.column.getCanSort() && (
                          <span className="text-fg-subtle">
                            {{
                              asc: <ChevronUp className="w-3.5 h-3.5" />,
                              desc: <ChevronDown className="w-3.5 h-3.5" />,
                            }[header.column.getIsSorted() as string] ?? (
                              <ChevronsUpDown className="w-3.5 h-3.5" />
                            )}
                          </span>
                        )}
                      </div>
                    </th>
                  )
                })
              )}
            </tr>
            {showColumnFilters && (
              <tr className="bg-surface-raised/50 border-b border-line">
                {table.getHeaderGroups().map((headerGroup) =>
                  headerGroup.headers.map((header) => (
                    <th key={header.id} className="px-2 py-1.5">
                      <ColumnFilter column={header.column} />
                    </th>
                  ))
                )}
              </tr>
            )}
          </thead>
          <tbody className="divide-y divide-line">
            {table.getRowModel().rows.length > 0 ? (
              table.getRowModel().rows.map((row) => {
                const isActive = activeRowId != null && row.id === activeRowId
                return (
                  <Fragment key={row.id}>
                    <tr
                      onClick={(e) => handleRowClick(e, row.original)}
                      className={cn(
                        'h-9 transition-colors',
                        onRowClick && 'cursor-pointer',
                        isActive
                          ? 'bg-primary-soft shadow-[inset_2px_0_0_var(--color-primary)]'
                          : row.getIsSelected()
                            ? 'bg-primary-soft/40 hover:bg-primary-soft/60'
                            : 'hover:bg-surface-raised/60'
                      )}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td
                          key={cell.id}
                          className={cn(
                            'px-3 h-9 align-middle text-fg overflow-hidden text-ellipsis whitespace-nowrap',
                            cell.column.columnDef.meta?.align === 'right' && 'text-right tabular-nums'
                          )}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                    {renderExpandedRow && row.getIsExpanded() && (
                      <tr>
                        <td colSpan={row.getVisibleCells().length}>
                          {renderExpandedRow(row.original)}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })
            ) : (
              <tr>
                {/* Centred in the visible width, not the table's, so a phone scrolled to the left still sees it. */}
                <td colSpan={visibleColumnCount} className="p-0">
                  <div className="sticky left-0 w-[100cqw] px-3 py-10 text-center text-fg-muted">{emptyMessage}</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Footer: bulk actions or paging summary */}
      {totalRows > 0 && (
        <div
          className={cn(
            '@container flex items-center justify-between flex-wrap gap-x-4 gap-y-2 px-3 py-2 border-t border-line text-xs',
            selectedRows.length > 0 && bulkActions ? 'bg-primary-soft text-primary-soft-fg' : 'text-fg-muted'
          )}
        >
          <div className="flex items-center gap-3 min-w-0 flex-wrap">
            {selectedRows.length > 0 && bulkActions ? (
              <>
                <span className="font-semibold whitespace-nowrap">{selectedRows.length} selected</span>
                {bulkActions(selectedRows, () => table.resetRowSelection())}
              </>
            ) : (
              <>
                <span className="whitespace-nowrap">
                  <span className="hidden @sm:inline">Showing </span>
                  {(currentPageIndex * pageSize + 1).toLocaleString()}-{Math.min((currentPageIndex + 1) * pageSize, totalRows).toLocaleString()} of {totalRows.toLocaleString()}{totalExact ? '' : '+'}
                  {showColumnFilters && columnFilters.length > 0 && (
                    <span className="text-fg-subtle"> (filtered)</span>
                  )}
                </span>
                {!totalExact && countAll && (
                  <button
                    onClick={countAll.onCount}
                    disabled={countAll.counting}
                    className="text-primary hover:brightness-110 disabled:opacity-60 disabled:cursor-wait"
                    title="Count every matching row. Under a filter without an index this reads the whole table."
                  >
                    {countAll.counting ? 'Counting…' : 'Count all'}
                  </button>
                )}
                {countAll?.error && <span className="text-danger">{countAll.error}</span>}
                {showColumnFilters && columnFilters.length > 0 && (
                  <button
                    onClick={() => setColumnFilters([])}
                    className="flex items-center gap-1 text-primary hover:brightness-110"
                  >
                    <X className="w-3 h-3" />
                    Clear filters
                  </button>
                )}
              </>
            )}
          </div>

          <div className="flex items-center gap-3 flex-none ml-auto">
            <label className="flex items-center gap-1.5" title="Rows per page">
              <span className="hidden @sm:inline">Rows</span>
              <select
                value={pageSize}
                onChange={(e) => table.setPageSize(Number(e.target.value))}
                className="px-1.5 py-0.5 border border-line-strong rounded-sm bg-surface text-fg text-xs focus:outline-hidden focus:ring-1 focus:ring-ring"
              >
                {[10, 20, 50, 100].map((size) => (
                  <option key={size} value={size}>{size}</option>
                ))}
              </select>
            </label>
            <div className="flex items-center gap-1">
              <PagerButton onClick={() => table.setPageIndex(0)} disabled={!table.getCanPreviousPage()} title="First page" className="hidden @sm:block"><ChevronsLeft className="w-4 h-4" /></PagerButton>
              <PagerButton onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()} title="Previous page"><ChevronLeft className="w-4 h-4" /></PagerButton>
              <span className="px-1 tabular-nums text-fg">{(currentPageIndex + 1).toLocaleString()}<span className="text-fg-subtle"> / {pageCount.toLocaleString()}{totalExact ? '' : '+'}</span></span>
              <PagerButton onClick={() => table.nextPage()} disabled={!table.getCanNextPage()} title="Next page"><ChevronRight className="w-4 h-4" /></PagerButton>
              <PagerButton onClick={() => table.setPageIndex(pageCount - 1)} disabled={!table.getCanNextPage() || !totalExact} title={totalExact ? 'Last page' : 'Last page (count all first)'} className="hidden @sm:block"><ChevronsRight className="w-4 h-4" /></PagerButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function PagerButton({ children, className, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className={cn('p-1 rounded-sm text-fg-muted hover:bg-surface-raised hover:text-fg disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent', className)}
      {...props}
    >
      {children}
    </button>
  )
}

const filterInputClasses =
  'w-full px-1.5 py-1 text-xs border border-line-strong rounded-sm bg-surface text-fg focus:outline-hidden focus:ring-1 focus:ring-ring placeholder:text-fg-subtle'

function ColumnFilter<TData extends RowData>({ column }: { column: Column<DataTableFeatures, TData, unknown> }) {
  const filterVariant = column.columnDef.meta?.filterVariant
  const fixedOptions = column.columnDef.meta?.filterOptions
  // Only faceted (select) filters need the unique-value model, so other columns skip building it.
  const facets = filterVariant === 'select' && !fixedOptions ? column.getFacetedUniqueValues() : null
  const options = useMemo(
    () =>
      fixedOptions ??
      (facets
        ? Array.from(facets.keys())
            .filter((v) => v !== undefined && v !== null && v !== '')
            .sort()
            .map((v) => ({ value: String(v), label: String(v) }))
        : []),
    [fixedOptions, facets],
  )
  if (!filterVariant) return null

  if (filterVariant === 'select') {
    return (
      <select
        value={(column.getFilterValue() as string) ?? ''}
        onChange={(e) => column.setFilterValue(e.target.value || undefined)}
        className={filterInputClasses}
      >
        <option value="">All</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    )
  }

  return (
    <input
      type="text"
      value={(column.getFilterValue() as string) ?? ''}
      onChange={(e) => column.setFilterValue(e.target.value || undefined)}
      placeholder="Filter…"
      className={filterInputClasses}
    />
  )
}

// Selection column with "select all pages" support
export function createSelectionColumn<TData extends RowData>(): ColumnDef<DataTableFeatures, TData, unknown> {
  return {
    id: 'select',
    size: 40,
    header: ({ table }) => {
      const isAllPageRowsSelected = table.getIsAllPageRowsSelected()
      const isSomeRowsSelected = table.getIsSomeRowsSelected()
      const totalRows = table.getFilteredRowModel().rows.length
      const pageRows = table.getRowModel().rows.length
      const hasMultiplePages = totalRows > pageRows

      return (
        <div className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={isAllPageRowsSelected}
            ref={(el) => {
              if (el) {
                el.indeterminate = table.getIsSomePageRowsSelected() && !isAllPageRowsSelected
              }
            }}
            onChange={table.getToggleAllPageRowsSelectedHandler()}
            className="w-4 h-4 rounded-sm border-line-strong bg-surface text-primary focus:ring-ring cursor-pointer"
            aria-label="Select all on page"
          />
          {hasMultiplePages && (
            <div className="relative group">
              <button
                type="button"
                className="p-0.5 text-fg-subtle hover:text-fg-muted"
                aria-label="Selection options"
              >
                <ChevronDown className="w-3 h-3" />
              </button>
              {/* group-[:hover], not group-hover: Tailwind's hover variants only apply where the pointer can
                  hover, and a tap must still open this menu on touch screens. */}
              <div className="absolute left-0 top-full mt-1 bg-surface border border-line-strong rounded-sm shadow-lg opacity-0 invisible group-[:hover]:opacity-100 group-[:hover]:visible transition-all z-50 whitespace-nowrap normal-case tracking-normal font-normal">
                <button
                  type="button"
                  onClick={() => table.toggleAllPageRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-surface-raised"
                >
                  Select page ({pageRows})
                </button>
                <button
                  type="button"
                  onClick={() => table.toggleAllRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-surface-raised"
                >
                  Select all ({totalRows})
                </button>
                {isSomeRowsSelected && (
                  <button
                    type="button"
                    onClick={() => table.toggleAllRowsSelected(false)}
                    className="block w-full px-3 py-1.5 text-left text-xs hover:bg-surface-raised text-danger"
                  >
                    Clear selection
                  </button>
                )}
              </div>
            </div>
          )}
        </div>
      )
    },
    cell: ({ row }) => (
      <input
        type="checkbox"
        checked={row.getIsSelected()}
        disabled={!row.getCanSelect()}
        onChange={row.getToggleSelectedHandler()}
        className="w-4 h-4 rounded-sm border-line-strong bg-surface text-primary focus:ring-ring cursor-pointer"
        aria-label="Select row"
      />
    ),
    enableSorting: false,
    enableHiding: false,
  }
}
