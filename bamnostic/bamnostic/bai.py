#!/user/bin/env python
# -*- coding: utf-8 -*-
'''BAI file parser 

The Binary Alignment Map (BAM) format (defined at https://samtools.github.io/hts-specs/SAMv1.pdf)
allows for indexing. When the user invokes a tool to index a given BAM file, a BAM index (BAI)
file is created. Generally speaking, a BAI contains the all the virtual offsets of clusters of
reads that are associated with specific subsections of the BAM file. When the BAI is produced,
random access to the BAM is available. 

This script is for parsing the binary encoded BAI file, inherently making it human readable. 
Furthermore, it allows subsections of the BAI to be loaded into memory, reducing the memory
footprint and speeding up queries within a small number of references. Lastly, by parsing it
as such, random access queries directly into the associated BAM file is available to other
tools within bamnostic

@author: "Marcus D. Sherman"
@copyright: "Copyright 2018, University of Michigan, Mills Lab
@email: "mdsherman<at>betteridiot<dot>tech"
@version: "v0.5.0"
'''
from __future__ import print_function
from __future__ import absolute_import
from __future__ import division

import struct
import os
import warnings
from collections import namedtuple
from functools import lru_cache

from bamnostic.utils import *

def format_warnings(message, category, filename, lineno, file=None, line=None):
    return ' {}:{}: {}:{}'.format(filename, lineno, category.__name__, message)

warnings.formatwarning = format_warnings

# Namedtuples for future-proofing and readability
RefIdx = namedtuple('RefIdx', ('start_offset', 'end_offset', 'n_bins'))
Bin = namedtuple('Bin', ('bin_id', 'chunks'))
Chunk = namedtuple('Chunk', ('voffset_beg', 'voffset_end'))
Ref = namedtuple('Ref', ('bins', 'intervals'))
Unmapped = namedtuple('Unmapped', ('unmmaped_beg', 'unmapped_end', 'n_mapped', 'n_unmapped'))


def reg2bin(beg: int, end: int) -> int:
    '''Finds the largest superset bin of region. Numeric values taken from hts-specs
    
    Args:
        beg (int): inclusive beginning position of region
        end (int): exclusive end position of region
    
    Returns:
        (int): distinct bin ID for largest superset bin of region
    '''
    left_shift = 15
    for i in range(14, 27, 3):
        if beg >> i == (end-1) >> i: 
            return int(((1<<left_shift)-1)/7 + (beg >> i))
        left_shift -= 3
    else:
        return 0

def reg2bins(rbeg: int, rend: int):
    '''Generates bin ids which overlap the specified region.
    
    Args:
        beg (int): inclusive beginning position of region
        end (int): exclusive end position of region
    
    Yields:
        (int): bin IDs for overlapping bins of region
    
    Raises:
        AssertionError (Exception): if the range is malformed or invalid
    '''
    # Based off the algorithm presented in:
    # https://samtools.github.io/hts-specs/SAMv1.pdf
    
    # Bin calculation constants.
    BIN_ID_STARTS = (0, 1, 9, 73, 585, 4681)
    
    # Maximum range supported by specifications.
    MAX_RNG = (2 ** 29) - 1
    
    assert 0 <= rbeg <= rend <= MAX_RNG, 'Invalid region {}, {}'.format(rbeg, rend)
    
    for start, shift in zip(BIN_ID_STARTS, range(29,13, -3)):
        i = rbeg >> shift if rbeg > 0 else 0
        j = rend >> shift if rend < MAX_RNG else MAX_RNG >> shift
        
        for bin_id_offset in range(i, j+1):
            yield start + bin_id_offset


class Bai:
    ''' This class defines the bam index file object and its interface.
    
    The purpose of this class is the binary parsing of the bam index file (BAI) associated with 
    a given bam file. When queried, the Bai object identifies the bins of data that overlap the 
    requested region and directs which parts of the bam file contain it.
    
    Virtual offsets are processed using the following method:
    Beginning of compressed block = coffset = virtual offset >> 16
    Position within uncompressed block = uoffset = virtual offset ^ (coffset << 16)
    
    Attributes:
        _io (fileObject): opened BAI file object
        _LINEAR_INDEX_WINDOW (int): constant of the linear interval window size
        _UNMAP_BIN (int): constant for bin ID of unmapped read stats
        magic (bytes): first 4 bytes of file. Must be equal to b'BAI\x01'
        n_refs (int): number of references in BAI
        unmapped (dict): dictionary of the unmapped read stats by each reference
        current_ref (None|dict): dictionary of the current reference loaded into memory. 
                                It contains the a dictionary of bin IDs and their respective
                                chunks, and a list of linear intervals.
        ref_indices (dict): dictionary of reference ids and their start/stop offsets within the BAI file
        n_no_coord (None|int): if present in BAI, is the number of reads that have no coordinates
        _last_pos (int): used for indexing, the byte position of the file head.
        
    '''
    def __init__(self, filename):
        '''Initialization method
        
        Generates an "index" of the index. This gives us the byte positions of each chromosome 
        within the index file. Now, when a user queries over a specific chromosome, it pulls 
        out just the index information for that chromosome--not the whole genome.
        
        Args:
            filename (str): '/path/to/bam_file' that automatically adds the '.bai' suffix
        
        Raises:
            OSError (Exception): if the BAI file is not found or does not exist
            AssertionError (Exception): if BAI magic is not found
        '''
        if os.path.isfile(filename):
            self._io = open(filename, 'rb')
        else:
            raise OSError('{} not found. Please change check your path or index your BAM file'.format(filename))
        self._io = open(filename, 'rb')
        
        # Constant for linear index window size and unmapped bin id
        self._LINEAR_INDEX_WINDOW = 16384
        self._UNMAP_BIN = 37450
        
        self.magic, self.n_refs = unpack("<4sl", serf._io)
        assert self.magic == b'BAI\x01', 'Wrong BAI magic header'
        
        
        self.unmapped = {}
        self.current_ref = None
        
        # Capture the offsets for each reference within the index
        self.ref_indices = {ref: self.get_ref(ref, idx=True) for ref in range(self.n_refs)}
        
        # Get the n_no_coor if it is present
        nnc_dat = self._io.read(8)
        self.n_no_coor = unpack('<Q', nnc_dat) if nnc_dat else None
            
        self._last_pos = self._io.tell()

    
    def get_chunks(self, n_chunks):
        '''Simple generator for unpacking chunk data
        
        Chunks are defined as groupings of reads within a BAM file
        that share the same bin. A `Chunk` object in the context of 
        this function is a `namedtuple` that contains the virtual offsets
        for the beginning and end of each of these chunks. 
        
        Note: a special case of a chunk is in any Bin labeled as 37450.
        These bins always contain 2 chunks that provide the statistics
        of the number of reads that are unmapped to that reference.
        
        Args:
            n_chunks (int): number of chunks to be unpacked from stream
        
        Returns:
            chunks (list): a list of Chunk objects with the attributes of
                            chunks[i] are .voffset_beg and voffset_end
        '''
        chunks = [Chunk(*unpack('<2Q', self._io)) for chunk in range(n_chunks)]
        return chunks
    
    # TODO: Add a check for long reads and allow for skipping the linear index on queries
    def get_ints(self, n_int):
        '''Unpacks `n_int` number of interval virtual offsets from stream
        
        A linear interval is defined as a 16384 bp window along a given reference. The
        value stored in the BAI is the virtual offset of the first read within
        that given interval. This virtual offset is the byte offset (coffset) of the start of the
        BGZF block that contains the beginning of the read and the byte offset (uoffset)
        within the uncompressed data of the residing BGZF block to that first read. 
        
        Note: a caveat to using linear interval with long reads: A long read can
        span multiple linear intervals. As such, the current encoding could potentially shift
        the expected region of interest to the left more than expected.
        
        Args:
            n_int (int): number of intervals to unpack
        
        Returns:
            ints (list): list of virtual offsets for `n_int` number of linear intervals
        '''
        ints = [unpack('<Q', self._io) for i in range(n_int)]
        return ints
    
    def get_bins(self, n_bins, ref_id=None, idx = False):
        '''Simple function that iteratively unpacks the data of a number (`n_bin`) 
        of bins.
        
        As the function discovers the number of chunks needed for a given
        bin, it deletages work to `self.get_chunks(n_chunks)`. A bin is 
        comprised of 2 parts: 1) the distinct bin ID (within a given reference). If
        no reads are associated with that bin, it is left out of the indexing process,
        and therefore not represented in the BAI file. Furthermore, while each bin
        has a bin ID, the bin IDs are only distinct within a given reference. This means
        that 2 or more references can have the same bin IDs. These bin IDs are also
        not in any order as they are essentially a hash dump. Lastly, the only reserved
        bin ID is 37450. This bin relates to 2 chunks that contain the number of 
        unmapped and mapped reads for a given reference. 2) the chunk(s) of reads that
        are assigned to a given bin.
        
        As a secondary feature, this function will also quickly seek over regions for
        the purposes of documenting the start and stop byte offsets of a given reference
        block within the file. This is invoked by setting `idx=True`
        
        
        Args:
            n_int (int): number of bins to be unpacked from stream
        
        Returns:
            bins (None | dict): None if just indexing the index file or a dictionary
                                of `bin_id: chunks` pairs
        Raises:
            AssertionError (Exception): if bin 37450 does not contain 2 chunks exactly
        '''
        bins = None if idx else {}
        
        for b in range(n_bins):
            bin_id, n_chunks = unpack('<Ii', self._io)
            
            if idx:
                if bin_id == self._UNMAP_BIN:
                    assert n_chunks == 2, 'Bin 3740 is supposed to have 2 chunks. This has {}'.format(n_chunks)
                    unmapped = Unmapped(*unpack('<4Q', self._io))
                    self.unmapped[ref_id] = unmapped
                else:
                    if not n_chunks == 0:
                        self._io.seek(struct.calcsize('<2Q') * n_chunks, 1)
            else:
                chunks = self.get_chunks(n_chunks)
                bins[bin_id] = chunks
        else:
            return bins
    
    # Cache the references to speed up queries. 
    @lru_cache(4)
    def get_ref(self, ref_id=None, idx = False):
        '''Interatively unpacks all the bins, linear intervals, and chunks for a given reference
        
        A reference is comprised of 2 things: 1) a series of bins that reference chunks of aligned
        reads that are grouped within that bin. 2) a series of virtual offsets of the first read of a
        16384 bp window along the given reference.
        
        This function also serves to "index" the BAI file such that, if it is invoked by setting 
        `ids=True`, will do a single pass through the BAI file and saving the start and stop
        offsets of each of the references. This is used for minimizing the memory footprint of 
        storing the BAI in memory. When queried against, the appropriate reference block will be 
        loaded. Because of this constant loading, `functools.lru_cache` was applied to cache recently
        used reference blocks to speed up computation. It is assumed that when querying is done, most
        users are looking and just a few references at a time.
        
        Args:
            ref_id (None|int): used for random access or indexing the BAI
            idx (bool): Flag for setting whether or not to run an index of the BAI
        
        Returns:
            RefIdx: `namedtuple` containing the byte offsets of the reference start, stop, and number of bins
            or
            Ref: `namedtuple` containing a dictionary of bins and list of linear intervals
        
        Raises:
            AssertionError (Exception): if, when random access is used, the current reference offset
                                        does not match indexed reference offset.
        '''
        if ref_id is not None and not idx:
            ref_start, _, _ = self.ref_indices[ref_id]
            self._io.seek(ref_start)
        ref_start = self._io.tell()
        
        if not idx:
            assert ref_start == self.ref_indices[ref_id].start_offset, 'ref not properly aligned'
        
        n_bins = unpack('<l', self._io)
        bins = self.get_bins(n_bins, ref_id, idx)
        
        n_int = unpack('<l', self._io)
        if idx:
            self._io.seek(struct.calcsize('<Q') * n_int, 1)
        
        ints = None if idx else self.get_ints(n_int)
        
        self._last_pos = self._io.tell()
        
        if idx:
            return RefIdx(ref_start, self._last_pos, n_bins)
        else:
            return Ref(bins, ints)
    
    
    def query(self, ref_id, start, stop):
        ''' Main query function for yielding AlignedRead objects from specified region
        
        Args:
            ref (int): which reference/chromosome TID
            start (int): left most bp position of region (zero-based)
            stop (int): right most bp position of region (zero-based)
        
        Yields:
            (int): all Chunk objects within region of interest
        '''
        self.current_ref = self.get_ref(ref_id)
        
        
        start_offset = self.current_ref['intervals'][start // self.LINEAR_INDEX_WINDOW]
        try:
            end_offset = self.current_ref['intervals'][ceildiv(stop, self._LINEAR_INDEX_WINDOW)]
        except IndexError:
            # This is used in case the user wants all the offsets for an entire contig.
            # Takes into account the length of the contig (as per @SQ LN) and finds
            # the largest possible bin. Since coverage does not extend the full 
            # length of the chromosome, have to select the highest possible linear
            # index. Furthermore, to make it work with existing predicates, it needs
            # to be offset by 1.
            end_offset = self.current_ref['intervals'][-1] + 1
        
        current_start = None
        current_end = None
        
        for binID in reg2bins(start, stop):
            try:
                bin_chunks = self.current_ref['bins'][binID]
            except KeyError:
                continue
            
            for chunk in bin_chunks:
                if (start_offset <= chunk.voffset_beg) and (chunk.voffset_end < end_offset):
                    if not current_start:
                        current_start = chunk.voffset_beg
                        current_end = chunk.voffset_end
                    elif chunk.voffset_beg == current_end:
                        current_end = chunk.voffset_end
                    else:
                        yield (current_start, current_end)
                        current_start = chunk.voffset_beg
                        current_end = chunk.voffset_end
        else:
            yield (current_start, current_end)
    
    def seek(self, offset = None, whence = 0):
        '''Simple seek function for binary files
        
        Args:
            offset (None|int): byte offset from whence to move the file head to.
            whence (int): 0 := from start of file, 1:= from current position, 2:= from end of file
        
        Returns:
            (int): new byte position of file head
        
        Raise:
            ValueError (Exception): if the offset is not an integer or is not provided
        '''
        if isinstance(offset, (int, None)):
            if offset is None:
                raise ValueError('No offset provided')
            else:
                self._io.seek(offset, whence)
                return self._io.tell()
        else:
            raise ValueError('offset must be an integer or None')
    
    def read(self, size = -1):
        '''Simple read function for binary files
        
        Args:
            size (int): number of bytes to read in (default: -1 --whole file)
        
        Returns:
            (bytes): the number of bytes read from file
        '''
        if size == 0:
            return b''
        else:
            return self._io.read(size)
            
    def tell(self):
        '''Simple tell function for reporting byte position of file head
        
        Returns:
            (int): byte position of file head
        '''
        return self._io.tell()