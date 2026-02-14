import { useState, useEffect } from 'react'
import {
  ColumnDef,
  SortingState,
  RowSelectionState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { ChevronUp, ChevronDown, ChevronsUpDown, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react'

interface DataTableProps<TData, TValue> {
  columns: ColumnDef<TData, TValue>[]
  data: TData[]
  defaultPageSize?: number
  emptyMessage?: string
  enableRowSelection?: boolean
  onSelectionChange?: (selectedRows: TData[]) => void
  getRowId?: (row: TData) => string
}

export function DataTable<TData, TValue>({
  columns,
  data,
  defaultPageSize = 20,
  emptyMessage = 'No data available.',
  enableRowSelection = false,
  onSelectionChange,
  getRowId,
}: DataTableProps<TData, TValue>) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({})

  const table = useReactTable({
    data,
    columns,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    enableRowSelection,
    getRowId,
    state: {
      sorting,
      rowSelection,
    },
    initialState: {
      pagination: {
        pageSize: defaultPageSize,
      },
    },
  })

  // Notify parent of selection changes
  useEffect(() => {
    if (onSelectionChange) {
      const selectedRows = table.getFilteredSelectedRowModel().rows.map(row => row.original)
      onSelectionChange(selectedRows)
    }
  }, [rowSelection, onSelectionChange, table])

  const pageIndex = table.getState().pagination.pageIndex
  const pageSize = table.getState().pagination.pageSize
  const totalRows = table.getFilteredRowModel().rows.length
  const pageCount = table.getPageCount()

  return (
    <div className="space-y-3">
      <div className="bg-white rounded-lg shadow overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-gray-50 border-b border-gray-200">
              {table.getHeaderGroups().map((headerGroup) =>
                headerGroup.headers.map((header) => (
                  <th
                    key={header.id}
                    className={`px-3 py-2 text-left text-xs font-semibold text-gray-600 uppercase tracking-wider ${
                      header.column.getCanSort() ? 'cursor-pointer select-none hover:bg-gray-100' : ''
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
          </thead>
          <tbody className="divide-y divide-gray-100">
            {table.getRowModel().rows.length > 0 ? (
              table.getRowModel().rows.map((row) => (
                <tr
                  key={row.id}
                  className={`hover:bg-gray-50 transition-colors ${
                    row.getIsSelected() ? 'bg-blue-50' : ''
                  }`}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className="px-3 py-2 text-gray-700">
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))
            ) : (
              <tr>
                <td colSpan={columns.length} className="px-3 py-8 text-center text-gray-500">
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
          <div className="flex items-center gap-4 text-gray-600">
            <span>
              Showing {pageIndex * pageSize + 1}-{Math.min((pageIndex + 1) * pageSize, totalRows)} of {totalRows}
            </span>
            <div className="flex items-center gap-2">
              <label htmlFor="pageSize" className="text-gray-500">Rows:</label>
              <select
                id="pageSize"
                value={pageSize}
                onChange={(e) => table.setPageSize(Number(e.target.value))}
                className="px-2 py-1 border border-gray-300 rounded text-sm focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                {[10, 20, 50, 100].map((size) => (
                  <option key={size} value={size}>{size}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex items-center gap-1 text-gray-600">
              <span>Page</span>
              <input
                type="number"
                min={1}
                max={pageCount}
                value={pageIndex + 1}
                onChange={(e) => {
                  const page = e.target.value ? Number(e.target.value) - 1 : 0
                  table.setPageIndex(Math.max(0, Math.min(page, pageCount - 1)))
                }}
                className="w-14 px-2 py-1 border border-gray-300 rounded text-sm text-center focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <span>of {pageCount}</span>
            </div>

            <div className="flex items-center gap-1 ml-2">
              <button
                onClick={() => table.setPageIndex(0)}
                disabled={!table.getCanPreviousPage()}
                className="p-1 rounded hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                title="First page"
              >
                <ChevronsLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.previousPage()}
                disabled={!table.getCanPreviousPage()}
                className="p-1 rounded hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Previous page"
              >
                <ChevronLeft className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.nextPage()}
                disabled={!table.getCanNextPage()}
                className="p-1 rounded hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
                title="Next page"
              >
                <ChevronRight className="w-4 h-4" />
              </button>
              <button
                onClick={() => table.setPageIndex(pageCount - 1)}
                disabled={!table.getCanNextPage()}
                className="p-1 rounded hover:bg-gray-100 disabled:opacity-40 disabled:cursor-not-allowed"
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

// Helper function to create a selection column with "select all pages" support
export function createSelectionColumn<TData>(): ColumnDef<TData, unknown> {
  return {
    id: 'select',
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
            className="w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 cursor-pointer"
            aria-label="Select all on page"
          />
          {hasMultiplePages && (
            <div className="relative group">
              <button
                type="button"
                className="p-0.5 text-gray-400 hover:text-gray-600"
                aria-label="Selection options"
              >
                <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                </svg>
              </button>
              <div className="absolute left-0 top-full mt-1 bg-white border border-gray-200 rounded shadow-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all z-50 whitespace-nowrap">
                <button
                  type="button"
                  onClick={() => table.toggleAllPageRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100"
                >
                  Select page ({pageRows})
                </button>
                <button
                  type="button"
                  onClick={() => table.toggleAllRowsSelected(true)}
                  className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100"
                >
                  Select all ({totalRows})
                </button>
                {(isAllRowsSelected || isSomeRowsSelected) && (
                  <button
                    type="button"
                    onClick={() => table.toggleAllRowsSelected(false)}
                    className="block w-full px-3 py-1.5 text-left text-xs hover:bg-gray-100 text-red-600"
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
        className="w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500 cursor-pointer"
        aria-label="Select row"
      />
    ),
    enableSorting: false,
    enableHiding: false,
  }
}
