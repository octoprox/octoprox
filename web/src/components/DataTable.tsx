// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Fragment, useState, useEffect, useMemo, type ReactNode } from 'react'
import {
  ColumnDef,
  ColumnFiltersState,
  Column,
  ExpandedState,
  SortingState,
  RowSelectionState,
  VisibilityState,
  FilterFn,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  getFacetedRowModel,
  getFacetedUniqueValues,
  getSortedRowModel,
  getPaginationRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronUp, ChevronDown, ChevronsUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, X } from 'lucide-react'
import { cn } from '../utils/cn'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends unknown, TValue> {
    filterVariant?: 'text' | 'select' | 'range'
    /** Right-align numeric columns. */
    align?: 'left' | 'right'
  }
  interface FilterFns {
    range: FilterFn<unknown>
  }
}

const rangeFilterFn: FilterFn<unknown> = (row, columnId, filterValue: [number | '', number | '']) => {
  const value = row.getValue<number>(columnId)
  const [min, max] = filterValue
  if (min !== '' && value < min) return false
  if (max !== '' && value > max) return false
  return true
}

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[]
  data: TData[]
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
  columnVisibility?: VisibilityState
  /** Renders in the footer while rows are selected. */
  bulkActions?: (selected: TData[], clearSelection: () => void) => ReactNode
  className?: string
}

export function DataTable<TData, TValue>({
  columns,
  data,
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
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([])
  const [expanded, setExpanded] = useState<ExpandedState>({})

  const table = useReactTable({
    data,
    columns,
    filterFns: { range: rangeFilterFn },
    getCoreRowModel: getCoreRowModel(),
    getExpandedRowModel: renderExpandedRow ? getExpandedRowModel() : undefined,
    getRowCanExpand: renderExpandedRow ? () => true : undefined,
    getFilteredRowModel: getFilteredRowModel(),
    getFacetedRowModel: getFacetedRowModel(),
    getFacetedUniqueValues: getFacetedUniqueValues(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    onColumnFiltersChange: setColumnFilters,
    onExpandedChange: setExpanded,
    enableRowSelection,
    getRowId,
    autoResetPageIndex: false,
    state: {
      sorting,
      rowSelection,
      columnFilters,
      expanded,
      ...(columnVisibility ? { columnVisibility } : {}),
    },
    initialState: {
      pagination: {
        pageSize: defaultPageSize,
      },
    },
  })

  // If the current page becomes invalid after data changes, go to the last page.
  const pageCount = table.getPageCount()
  const currentPageIndex = table.getState().pagination.pageIndex
  useEffect(() => {
    if (pageCount > 0 && currentPageIndex >= pageCount) {
      table.setPageIndex(pageCount - 1)
    }
  }, [pageCount, currentPageIndex, table])

  const selectedRows = table.getFilteredSelectedRowModel().rows.map((row) => row.original)
  useEffect(() => {
    onSelectionChange?.(selectedRows)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rowSelection, onSelectionChange])

  const pageSize = table.getState().pagination.pageSize
  const totalRows = table.getFilteredRowModel().rows.length
  const visibleColumnCount = table.getVisibleLeafColumns().length

  const handleRowClick = (e: React.MouseEvent, original: TData) => {
    if (!onRowClick) return
    const target = e.target as HTMLElement
    if (target.closest('button, a, input, select, textarea, label, [data-no-row-click]')) return
    onRowClick(original)
  }

  return (
    <div className={cn('bg-surface rounded-lg border border-line overflow-hidden min-w-0 flex flex-col', className)}>
      <div className="overflow-x-auto">
        <table className="w-full text-[13px] table-fixed">
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
            {enableColumnFilters && (
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
                          ? 'bg-primary-soft shadow-[inset_2px_0_0_rgb(var(--color-primary))]'
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
                <td colSpan={visibleColumnCount} className="px-3 py-10 text-center text-fg-muted">
                  {emptyMessage}
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
            'flex items-center justify-between gap-4 px-3 py-2 border-t border-line text-xs',
            selectedRows.length > 0 && bulkActions ? 'bg-primary-soft text-primary-soft-fg' : 'text-fg-muted'
          )}
        >
          <div className="flex items-center gap-3 min-w-0">
            {selectedRows.length > 0 && bulkActions ? (
              <>
                <span className="font-semibold whitespace-nowrap">{selectedRows.length} selected</span>
                {bulkActions(selectedRows, () => table.resetRowSelection())}
              </>
            ) : (
              <>
                <span className="whitespace-nowrap">
                  Showing {currentPageIndex * pageSize + 1}–{Math.min((currentPageIndex + 1) * pageSize, totalRows)} of {totalRows}
                  {enableColumnFilters && columnFilters.length > 0 && (
                    <span className="text-fg-subtle"> (filtered)</span>
                  )}
                </span>
                {enableColumnFilters && columnFilters.length > 0 && (
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

          <div className="flex items-center gap-3 flex-none">
            <label className="flex items-center gap-1.5">
              <span>Rows</span>
              <select
                value={pageSize}
                onChange={(e) => table.setPageSize(Number(e.target.value))}
                className="px-1.5 py-0.5 border border-line-strong rounded bg-surface text-fg text-xs focus:outline-none focus:ring-1 focus:ring-ring"
              >
                {[10, 20, 50, 100].map((size) => (
                  <option key={size} value={size}>{size}</option>
                ))}
              </select>
            </label>
            <div className="flex items-center gap-1">
              <PagerButton onClick={() => table.setPageIndex(0)} disabled={!table.getCanPreviousPage()} title="First page"><ChevronsLeft className="w-4 h-4" /></PagerButton>
              <PagerButton onClick={() => table.previousPage()} disabled={!table.getCanPreviousPage()} title="Previous page"><ChevronLeft className="w-4 h-4" /></PagerButton>
              <span className="px-1 tabular-nums text-fg">{currentPageIndex + 1}<span className="text-fg-subtle"> / {pageCount}</span></span>
              <PagerButton onClick={() => table.nextPage()} disabled={!table.getCanNextPage()} title="Next page"><ChevronRight className="w-4 h-4" /></PagerButton>
              <PagerButton onClick={() => table.setPageIndex(pageCount - 1)} disabled={!table.getCanNextPage()} title="Last page"><ChevronsRight className="w-4 h-4" /></PagerButton>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

function PagerButton({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      className="p-1 rounded text-fg-muted hover:bg-surface-raised hover:text-fg disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent"
      {...props}
    >
      {children}
    </button>
  )
}

const filterInputClasses =
  'w-full px-1.5 py-1 text-xs border border-line-strong rounded bg-surface text-fg focus:outline-none focus:ring-1 focus:ring-ring placeholder:text-fg-subtle'

function ColumnFilter<TData>({ column }: { column: Column<TData, unknown> }) {
  const filterVariant = column.columnDef.meta?.filterVariant
  // Only faceted (select) filters need the unique-value model; asking other columns throws.
  const facets = filterVariant === 'select' ? column.getFacetedUniqueValues() : null
  const sortedUniqueValues = useMemo(
    () => (facets ? Array.from(facets.keys()).filter((v) => v !== undefined && v !== null && v !== '').sort() : []),
    [facets],
  )
  if (!filterVariant) return null

  if (filterVariant === 'range') {
    const filterValue = (column.getFilterValue() as [number | '', number | '']) ?? ['', '']
    return (
      <div className="flex gap-1">
        <input
          type="number"
          value={filterValue[0]}
          onChange={(e) => column.setFilterValue([e.target.value === '' ? '' : Number(e.target.value), filterValue[1]])}
          placeholder="Min"
          className={filterInputClasses}
        />
        <input
          type="number"
          value={filterValue[1]}
          onChange={(e) => column.setFilterValue([filterValue[0], e.target.value === '' ? '' : Number(e.target.value)])}
          placeholder="Max"
          className={filterInputClasses}
        />
      </div>
    )
  }

  if (filterVariant === 'select') {
    return (
      <select
        value={(column.getFilterValue() as string) ?? ''}
        onChange={(e) => column.setFilterValue(e.target.value || undefined)}
        className={filterInputClasses}
      >
        <option value="">All</option>
        {sortedUniqueValues.map((value) => (
          <option key={String(value)} value={String(value)}>
            {String(value)}
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
export function createSelectionColumn<TData>(): ColumnDef<TData, unknown> {
  return {
    id: 'select',
    size: 40,
    header: ({ table }) => {
      const isAllRowsSelected = table.getIsAllRowsSelected()
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
            className="w-4 h-4 rounded border-line-strong bg-surface text-primary focus:ring-ring cursor-pointer"
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
              <div className="absolute left-0 top-full mt-1 bg-surface border border-line-strong rounded shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 whitespace-nowrap normal-case tracking-normal font-normal">
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
                {(isAllRowsSelected || isSomeRowsSelected) && (
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
        className="w-4 h-4 rounded border-line-strong bg-surface text-primary focus:ring-ring cursor-pointer"
        aria-label="Select row"
      />
    ),
    enableSorting: false,
    enableHiding: false,
  }
}
