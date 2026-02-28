// Copyright 2026 Octoprox Authors
// SPDX-License-Identifier: Apache-2.0

import { Fragment, useState, useEffect, useMemo } from 'react'
import {
  ColumnDef,
  ColumnFiltersState,
  Column,
  ExpandedState,
  SortingState,
  RowSelectionState,
  FilterFn,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  getFilteredRowModel,
  getFacetedUniqueValues,
  getSortedRowModel,
  getPaginationRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronUp, ChevronDown, ChevronsUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, X } from 'lucide-react'

declare module '@tanstack/react-table' {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends unknown, TValue> {
    filterVariant?: 'text' | 'select' | 'range'
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
  renderExpandedRow?: (row: TData) => React.ReactNode
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
    },
    initialState: {
      pagination: {
        pageSize: defaultPageSize,
      },
    },
  })

  // Handle edge case: if current page becomes invalid after data changes,
  // navigate to the last available page
  const pageCount = table.getPageCount()
  const currentPageIndex = table.getState().pagination.pageIndex
  useEffect(() => {
    if (pageCount > 0 && currentPageIndex >= pageCount) {
      table.setPageIndex(pageCount - 1)
    }
  }, [pageCount, currentPageIndex, table])

  // Notify parent of selection changes
  useEffect(() => {
    if (onSelectionChange) {
      const selectedRows = table.getFilteredSelectedRowModel().rows.map(row => row.original)
      onSelectionChange(selectedRows)
    }
  }, [rowSelection, onSelectionChange, table])

  const pageSize = table.getState().pagination.pageSize
  const totalRows = table.getFilteredRowModel().rows.length

  return (
    <div className="space-y-3 min-w-0">
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm table-fixed">
          <thead>
            <tr className="bg-gray-50 dark:bg-gray-700 border-b border-gray-200 dark:border-gray-600">
              {table.getHeaderGroups().map((headerGroup) =>
                headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    style={{ width: header.getSize() !== 150 ? header.getSize() : undefined }}
                    className={`px-3 py-2 text-left text-xs font-semibold text-gray-600 dark:text-gray-300 uppercase tracking-wider ${
                      header.column.getCanSort() ? 'cursor-pointer select-none hover:bg-gray-100 dark:hover:bg-gray-600' : ''
                    }`}
                    onClick={header.column.getToggleSortingHandler()}
                  >
                    <div className="flex items-center gap-1">
                      {header.isPlaceholder
                        ? null
                        : flexRender(header.column.columnDef.header, header.getContext())}
                      {header.column.getCanSort() && (
                        <span className="text-gray-400">
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
                ))
              )}
            </tr>
            {enableColumnFilters && (
              <tr className="bg-gray-50/50 dark:bg-gray-700/50 border-b border-gray-200 dark:border-gray-600">
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
          <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
            {table.getRowModel().rows.length > 0 ? (
              table.getRowModel().rows.map((row) => (
                <Fragment key={row.id}>
                  <tr
                    className={`h-10 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors ${
                      row.getIsSelected() ? 'bg-blue-50 dark:bg-blue-900/20' : ''
                    }`}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className="px-3 h-10 align-middle text-gray-700 dark:text-gray-300">
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
              ))
            ) : (
              <tr>
                <td colSpan={columns.length} className="px-3 py-8 text-center text-gray-500 dark:text-gray-400">
                  {emptyMessage}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination Controls */}
      {totalRows > 0 && (
        <div className="flex items-center justify-between text-sm">
          <div className="flex items-center gap-4 text-gray-600 dark:text-gray-400">
            <span>
              Showing {currentPageIndex * pageSize + 1}-{Math.min((currentPageIndex + 1) * pageSize, totalRows)} of {totalRows}
              {enableColumnFilters && columnFilters.length > 0 && (
                <span className="text-gray-400 dark:text-gray-500"> (filtered)</span>
              )}
            </span>
            {enableColumnFilters && columnFilters.length > 0 && (
              <button
                onClick={() => setColumnFilters([])}
                className="flex items-center gap-1 text-xs text-blue-600 dark:text-blue-400 hover:text-blue-800 dark:hover:text-blue-300"
              >
                <X className="w-3 h-3" />
                Clear filters
              </button>
            )}
            <div className="flex items-center gap-2">
              <label htmlFor="pageSize" className="text-gray-500 dark:text-gray-400">Rows:</label>
              <select
                id="pageSize"
                value={pageSize}
                onChange={(e) => table.setPageSize(Number(e.target.value))}
                className="px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                {[10, 20, 50, 100].map((size) => (
                  <option key={size} value={size}>{size}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 text-gray-600 dark:text-gray-400">
              <span>Page</span>
              <input
                type="number"
                min={1}
                max={pageCount}
                value={currentPageIndex + 1}
                onChange={(e) => {
                  const page = e.target.value ? Number(e.target.value) - 1 : 0
                  table.setPageIndex(Math.max(0, Math.min(page, pageCount - 1)))
                }}
                className="w-14 px-2 py-1 border border-gray-300 dark:border-gray-600 rounded text-sm text-center bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <span>of {pageCount}</span>
            </div>

            <div className="flex items-center gap-1 ml-2">
              <button
                onClick={() => table.setPageIndex(0)}
                disabled={!table.getCanPreviousPage()}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                title="First page"
              >
                <ChevronsLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Previous page"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Next page"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.setPageIndex(pageCount - 1)}
                disabled={!table.getCanNextPage()}
                className="p-1 rounded hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Last page"
              >
                <ChevronsRight className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

const filterInputClasses =
  'w-full px-1.5 py-1 text-xs border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500 placeholder-gray-400 dark:placeholder-gray-500'

function ColumnFilter<TData>({ column }: { column: Column<TData, unknown> }) {
  const filterVariant = column.columnDef.meta?.filterVariant
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
    const sortedUniqueValues = useMemo(
      () => Array.from(column.getFacetedUniqueValues().keys()).sort(),
      [column.getFacetedUniqueValues()],
    )
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

  // text filter
  return (
    <input
      type="text"
      value={(column.getFilterValue() as string) ?? ''}
      onChange={(e) => column.setFilterValue(e.target.value || undefined)}
      placeholder="Filter..."
      className={filterInputClasses}
    />
  )
}

// Helper function to create a selection column with "select all pages" support
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
            className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 dark:bg-gray-700 text-blue-600 focus:ring-blue-500 cursor-pointer"
            aria-label="Select all on page"
          />
          {hasMultiplePages && (
            <div className="relative group">
              <button
                type="button"
                className="p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                aria-label="Selection options"
              >
                <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <div className="absolute left-0 top-full mt-1 bg-white dark:bg-gray-700 border border-gray-200 dark:border-gray-600 rounded shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 whitespace-nowrap">
                <button
                  type="button"
                  onClick={() => table.toggleAllPageRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-600"
                >
                  Select page ({pageRows})
                </button>
                <button
                  type="button"
                  onClick={() => table.toggleAllRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-600"
                >
                  Select all ({totalRows})
                </button>
                {(isAllRowsSelected || isSomeRowsSelected) && (
                  <button
                    type="button"
                    onClick={() => table.toggleAllRowsSelected(false)}
                    className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 dark:hover:bg-gray-600 text-red-600"
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
        className="w-4 h-4 rounded border-gray-300 dark:border-gray-600 dark:bg-gray-700 text-blue-600 focus:ring-blue-500 cursor-pointer"
        aria-label="Select row"
      />
    ),
    enableSorting: false,
    enableHiding: false,
  }
}
