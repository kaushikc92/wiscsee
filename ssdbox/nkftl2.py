import bidict
import copy
from collections import deque
import datetime
import Queue
import itertools

import config
import ftlbuilder
import recorder
from utilities import utils
from .bitmap import FlashBitmap2
from ssdbox.devblockpool import *
from ftlsim_commons import *

from . import blkpool

"""
############## Checklist ###############
When you conduct an operation, consider how it affects the following data
structure:
1. OOB
2. Flash
3. Block Pool
4. DataBlockMappingTable
5. LogBlockMappingTable
6. LogPageMappingTable
7. Garbage Collector
8. Appending points


############## TODO: ####################
1. Test partial merge

############# Refactor NKFTL ############
- All physical block number varaible names end with _pbn
- All logical block number varaible names end with _lbn
- Each class should minimize interaction with other class, it should provide
interface for higher level class to implement
For example, mapping classes should not change block pool. Log mapping class
should not change data block mapping. Be modular!!!
If you find that you need information from another class, it is probably that
you are doing things in a level that is too low.
- Data structure should be easier to maintain. No spread-out data structures

"""

ERR_NEED_NEW_BLOCK, ERR_NEED_MERGING = ('ERR_NEED_NEW_BLOCK', 'ERR_NEED_MERGING')
DATA_USER = "data.user"
IN_LOG_BLOCK = "IN_LOG_BLOCK"
IN_DATA_BLOCK = "IN_DATA_BLOCK"
TYPE_LOG_BLOCK, TYPE_DATA_BLOCK = ('TYPE_LOG_BLOCK', 'TYPE_DATA_BLOCK')

global_debug = False

TAG_PARTIAL_MERGE   = 'PARTIAL.MERGE'
TAG_TRY_GC          = 'TRY.GC'
TAG_SWITCH_MERGE    = 'SWITCH.MERGE'
TAG_FULL_MERGE      = 'FULL.MERGE'
TAG_FORGROUND       = 'FORGROUND'
TAG_WRITE_DRIVEN    = 'WRITE.DRIVEN.DIRECT.ERASE'
TAG_THRESHOLD_GC    = 'THRESHOLD.GC.DIRECT.ERASE'

class OutOfSpaceError(RuntimeError):
    pass

class Config(config.ConfigNCQFTL):
    def __init__(self, confdic = None):
        super(Config, self).__init__(confdic)

        local_itmes = {
            ############## NKFTL (SAST) ############
            "nkftl": {
                'n_blocks_in_data_group': 4, # number of blocks in a data block group
                'max_blocks_in_log_group': 2, # max number of blocks in a log block group

                "GC_threshold_ratio": 0.8,
                "GC_low_threshold_ratio": 0.7,

                "provision_ratio": 1.5 # 1.5: 1GB user size, 1.5 flash size behind
            },
            'n_pages_per_region': self.n_pages_per_block,
        }
        self.update(local_itmes)

    def n_pages_per_region(self):
        return self['n_pages_per_region']

    def n_pages_per_data_group(self):
        n_blocks_in_data_group = self['nkftl']['n_blocks_in_data_group']
        n_pages_per_block = self.n_pages_per_block
        n_pages_per_dg = n_blocks_in_data_group * n_pages_per_block

        return n_pages_per_dg

    def nkftl_data_group_number_of_lpn(self, lpn):
        """
        Given lpn, return its data group number
        """
        dgn = (lpn / self.n_pages_per_block) / \
            self['nkftl']['n_blocks_in_data_group']
        return dgn

    def nkftl_data_group_number_of_logical_block(self, logical_block_num):
        dgn = logical_block_num / self['nkftl']['n_blocks_in_data_group']
        return dgn

    def nkftl_max_n_log_pages_in_data_group(self):
        """
        This is the max number of log pages in data group:
            max number of log blocks * number of pages in block
        """
        return self['nkftl']['max_blocks_in_log_group'] * \
            self.n_pages_per_block

    def region_id_of_lpn(self, lpn):
        return lpn / self['n_pages_per_region']

    def nkftl_allowed_num_of_data_blocks(self):
        """
        NKFTL has to have certain amount of log and data blocks
        Required data blocks =
        ((num of total block-1) * num of blocks in data group / (num of blocks
        in data group + num of blocks in a log group))

        -1 is because we need to at least one staging block for the purposes
        such as merging.
        """

        raise RuntimeError("nkftl_set_flash_num_blocks_by_data_block_bytes()"
            "should not be called anymore because it assumes the total number "
            "of data blocks and log blocks in flash to be proportional "
            "following N/K. In fact, the number of log blocks in flash can be "
            "less than total.flash.block * K/(N+K).")

    def nkftl_set_flash_num_blocks_by_data_block_bytes(self, data_bytes):
        """
        Example:
        data_byptes is the filesystem size (LBA size), and this will set
        the number of flash blocks based on the ratio of data blocks and
        log blocks.
        """

        raise RuntimeError("nkftl_set_flash_num_blocks_by_data_block_bytes()"
            "should not be called anymore because it assumes the total number "
            "of data blocks and log blocks in flash to be proportional "
            "following N/K. In fact, the number of log blocks in flash can be "
            "less than total.flash.block * K/(N+K).")



class GlobalHelper(object):
    """
    In case you need some global variables. We put all global stuff here so
    it is easier to manage. (And you know all the bad things you did :)
    """
    def __init__(self, confobj):
        # Sort of a counter incremented by lba operations
        self.cur_lba_op_timestamp = 0

    def incr_lba_op_timestamp(self):
        self.cur_lba_op_timestamp += 1


class OutOfBandAreas(object):
    def __init__(self, confobj):
        self.conf = confobj

        self.flash_num_blocks = confobj.n_blocks_per_dev
        self.flash_npage_per_block = confobj.n_pages_per_block
        self.total_pages = self.flash_num_blocks * self.flash_npage_per_block

        # Key data structures
        self.states = FlashBitmap2(confobj)
        self.ppn_to_lpn = {}

    def display_bitmap_by_block(self):
        npages_per_block = self.conf.n_pages_per_block
        nblocks = self.conf.n_blocks_per_dev
        totalpages =  nblocks * npages_per_block
        line = ''
        for i in range(totalpages):
            block_index = i / npages_per_block
            rem = i % npages_per_block
            if rem == 0:
                line += ' ' + str(block_index) + ':'
            line += str(self.states.page_state_human(i)) + '|'

        return line

    def translate_ppn_to_lpn(self, ppn):
        return self.ppn_to_lpn[ppn]

    def wipe_ppn(self, ppn):
        self.states.invalidate_page(ppn)

        # It is OK to delay deleting ppn_to_lpn[ppn] until we erase the block

    def erase_block(self, flash_block):
        """
        Note that it does not call flash.block_erase(), because that's not
        something OOB
        """
        self.states.erase_block(flash_block)

        start, end = self.conf.block_to_page_range(flash_block)
        for ppn in range(start, end):
            try:
                del self.ppn_to_lpn[ppn]
                # if you try to erase translation block here, it may fail,
                # but it is expected.
            except KeyError:
                pass

    def remap(self, lpn, old_ppn, new_ppn):
        """
        It remaps lpn from old_ppn to new_ppn
        mark the new_ppn as valid
        update the LPN in new page's OOB to lpn
        invalidate the old_ppn, so cleaner can GC it
        """
        self.states.validate_page(new_ppn)
        self.ppn_to_lpn[new_ppn] = lpn

        if old_ppn != None:
            # the lpn has mapping before this write
            self.states.invalidate_page(old_ppn)


    def lpns_of_block(self, flash_block):
        s, e = self.conf.block_to_page_range(flash_block)
        lpns = []
        for ppn in range(s, e):
            lpns.append(self.ppn_to_lpn.get(ppn, 'NA'))

        return lpns

    def is_any_page_valid(self, flash_block):
        """
        Check if there is any page in the
        """
        ppn_start, ppn_end = self.conf.block_to_page_range(flash_block)
        for ppn in range(ppn_start, ppn_end):
            if self.states.is_page_valid(ppn):
                return True
        return False



class MappingBase(object):
    """
    This class defines a __init__() that passes in necessary objects to the
    mapping object.
    """
    def __init__(self, confobj, recorderobj, global_helper_obj):
        self.conf = confobj
        self.recorder = recorderobj
        self.global_helper = global_helper_obj


class DataBlockMappingTable(MappingBase):
    def __init__(self, confobj, recorderobj, global_helper_obj):
        super(DataBlockMappingTable, self).__init__(confobj, recorderobj,
            global_helper_obj)

        self.logical_to_physical_block = bidict.bidict()

    def lbn_to_pbn(self, lbn):
        """
        Given a logical block number, it returns the corresponding physical
        block number, according to data block mapping

        Return Found, PBN
        """
        pbn = self.logical_to_physical_block.get(lbn, None)
        if pbn == None:
            return False, None
        else:
            return True, pbn

    def lpn_to_ppn(self, lpn):
        """
        Finds ppn of a lpn in data blocks>

        Note that the return ppn may not be valid. The caller needs to check.

        Return: found, ppn
        """
        logical_block, off = self.conf.page_to_block_off(lpn)
        found, pbn = self.lbn_to_pbn(logical_block)
        if not found:
            return False, None

        ppn = self.conf.block_off_to_page(pbn, off)
        return True, ppn

    def add_data_block_mapping(self, lbn, pbn):
        self.logical_to_physical_block[lbn] = pbn

    def remove_data_block_mapping(self, lbn):
        del self.logical_to_physical_block[lbn]

    def remove_data_block_mapping_by_pbn(self, pbn):
        del self.logical_to_physical_block.inv[pbn]

    def __str__(self):
        return str(self.logical_to_physical_block)


TDATA = 'TDATA'
TLOG = 'TLOG'
class NKBlockPool(MultiChannelBlockPoolBase):
    @property
    def freeblocks(self):
        blocks = self.get_blocks_of_tag(tag=TFREE)
        return blocks

    @property
    def log_usedblocks(self):
        blocks = self.get_blocks_of_tag(tag=TLOG)
        return blocks

    @property
    def data_usedblocks(self):
        blocks = self.get_blocks_of_tag(tag=TDATA)
        return blocks

    def pop_a_free_block_to_log_blocks(self):
        blocknum = self.pick_and_move(src=TFREE, dst=TLOG)
        return blocknum

    def move_used_log_to_data_block(self, blocknum):
        self.change_tag(blocknum, src=TLOG, dst=TDATA)

    def pop_a_free_block_to_data_blocks(self):
        blocknum = self.pick_and_move(src=TFREE, dst=TDATA)
        return blocknum

    def free_used_data_block(self, blocknum):
        self.change_tag(blocknum, src=TDATA, dst=TFREE)

    def free_used_log_block(self, blocknum):
        self.change_tag(blocknum, src=TLOG, dst=TFREE)

    def total_used_blocks(self):
        nfree = self.count_blocks(tag=TFREE)
        return self.n_blocks_per_dev - nfree

    def used_ratio(self):
        nfree = self.count_blocks(tag=TFREE)
        return (self.n_blocks_per_dev - nfree) / float(self.n_blocks_per_dev)


class LogGroup2(object):
    """
    - keep track of log blocks of this group
    - allocate pages from blocks of this group
    - report need to merge
    """
    def __init__(self, conf, block_pool, max_n_log_blocks):
        self.conf = conf
        self.block_pool = block_pool
        self.n_channels = block_pool.n_channels
        self.n_pages_per_block = block_pool.n_pages_per_block

        self.max_n_log_blocks = max_n_log_blocks
        # each channel has a current block or None
        self.log_channels = [[] for i in range(self.n_channels)]
        self._cur_channel = 0

        self._page_map = bidict.bidict() # lpn->ppn

    def update_block_use_time(self, blocknum):
        pass

    def clear(self):
        self._page_map.clear()
        self.log_channels = [[] for i in range(self.n_channels)]

    def add_mapping(self, lpn, ppn):
        """
        Note that this function may overwrite existing mapping. If later you
        need keeping everything, add one data structure.
        """
        blk, off = self.conf.page_to_block_off(ppn)
        assert blk in self.log_block_numbers()
        self._page_map[lpn] = ppn

    def remove_lpn(self, lpn):
        del self._page_map[lpn]

    def lpn_to_ppn(self, lpn):
        """
        return found, ppn
        """
        ppn = self._page_map.get(lpn, None)
        if ppn == None:
            return False, None
        else:
            return True, ppn

    def cur_blocks(self):
        ret = []
        for channel_id in range(self.n_channels):
            for cur_block in self.log_channels[channel_id]:
                ret.append(cur_block)
        return ret

    def log_block_numbers(self):
        ret = []
        for cur_block in self.cur_blocks():
            ret.append(cur_block.blocknum)
        return ret

    def _get_and_incr_cur_channel(self):
        channel = self._cur_channel
        self._cur_channel = (self._cur_channel + 1) % self.n_channels
        return channel

    def remove_log_block(self, log_pbn):
        # remove all page maps
        ppn_start, ppn_end = self.conf.block_to_page_range(log_pbn)
        for ppn in range(ppn_start, ppn_end):
            try:
                # del self._page_map[:ppn]
                del self._page_map.inv[ppn]
            except KeyError:
                pass

        self._remove_block(log_pbn)

    def _remove_block(self, blocknum):
        channel_id = blocknum / self.conf.n_blocks_per_channel
        channel_blocks = self.log_channels[channel_id]
        to_del = None
        for cur_block in channel_blocks:
            if cur_block.blocknum == blocknum:
                to_del = cur_block
                break
        channel_blocks.remove(to_del)

    def reached_max_log_blocks(self):
        return self.n_log_blocks() == self.max_n_log_blocks

    def n_log_blocks(self):
        total = 0
        for channel_id in range(self.n_channels):
            total += len(self.log_channels[channel_id])

        return total

    def n_free_pages(self):
        total = 0
        for channel_id in range(self.n_channels):
            total += self.n_channel_free_pages(channel_id)
        return total

    def n_channel_free_pages(self, channel_id):
        total = 0
        for cur_block in self.log_channels[channel_id]:
            total += cur_block.num_free_pages()
        return total

    def _allocate_block_in_channel(self, channel_id):
        cnt = self.block_pool.count_blocks(tag=TFREE, channels=[channel_id])
        if cnt < 1:
            return False

        if self.reached_max_log_blocks() is True:
            return False

        blocknum = self.block_pool.pick(tag=TFREE, channel_id=channel_id)
        self.block_pool.change_tag(blocknum, src=TFREE, dst=TLOG)
        self.log_channels[channel_id].append(
                CurrentBlock(self.n_pages_per_block, blocknum) )

        assert self.n_log_blocks() <= self.max_n_log_blocks, "{} > {}".format(
                self.n_log_blocks(), self.max_n_log_blocks)
        return True

    def _next_ppns_in_channel(self, n, channel_id):
        """
        This function only use the blocks already in this log group.
        It does it by best effort.
        """
        remaining = n
        cur_blocks = self.log_channels[channel_id]

        ppns = []
        for cur_block in cur_blocks:
            if remaining > 0:
                tmp_ppns = cur_block.next_ppns(remaining)
                ppns.extend(tmp_ppns)
                remaining -= len(tmp_ppns)

        return ppns

    def next_ppns(self, n, strip_unit_size):
        """
        It does it by best effort.
        - if it cannot allocate strip_unit_size pages in a channel, it will
          try to allocate. If the allocating fails, it will try using the
          available blocks.
        - It is possible that it cannot find n pages, it will return the pages
          that it has found. The caller will need to trigger GC to merge some
          log blocks and make new ones available.
        """
        remaining = n
        if strip_unit_size == 'infinity':
            strip_unit_size = float('inf')

        ret_ppns = []
        dead_channels = set()
        while remaining > 0 and len(dead_channels) < self.n_channels:
            # we do not need to check if number of log blocks exceed
            # max, because it is impossible. We check before we allocate
            # block.
            cur_channel_id = self._get_and_incr_cur_channel()
            reqsize = min(remaining, strip_unit_size)
            ppns = self._next_ppns_in_channel_with_allocation(
                    reqsize, cur_channel_id)
            ret_ppns.extend(ppns)
            remaining -= len(ppns)
            if len(ppns) < reqsize:
                # we cannot allocate more blocks in this channel because:
                # 1. no available blocks, or
                # 2. # of blocks reached max for this log group
                dead_channels.add(cur_channel_id)

        return ret_ppns

    def _next_ppns_in_channel_with_allocation(self, reqsize, channel_id):
        """
        Try allocate reqsize ppns from this channel, it may allocate more blocks

        Return: ppns
        """
        ret_ppns = []
        remaining = reqsize
        while remaining > 0:
            ppns = self._next_ppns_in_channel(remaining, channel_id=channel_id)
            ret_ppns.extend(ppns)
            remaining -= len(ppns)

            if remaining > 0:
                allocated = self._allocate_block_in_channel(channel_id)
                if allocated is False:
                    break

        return ret_ppns


class Translator(MappingBase):
    def __init__(self, confobj, recorderobj, global_helper_obj,
            log_mapping, data_block_mapping
            ):
        super(Translator, self).__init__(confobj, recorderobj,
            global_helper_obj)
        self.data_block_mapping_table = data_block_mapping
        self.log_mapping_table = log_mapping

    def __str__(self):
        ret = []
        ret.append('========================= MAPPING MANAGER ===========================')
        ret.append('------------------------ Data Block Mapping Table -------------------')
        ret.append(str(self.data_block_mapping_table))
        ret.append('------------------------ Log Block Mapping Table --------------------')
        ret.append(str(self.log_mapping_table))
        ret.append('=====================================================================')

        return '\n'.join(ret)

    def lpn_to_ppn(self, lpn):
        """
        This function tries to find the ppn of a lpn. It will try log blocks
        first. If not found, then it will try data blocks.

        Note that you need to check if the returned ppn is valid. This function
        only tells you if there is a valid mapping.

        (maybe you should not say found is True if it is not valid)

        Return: Found?, PPN, Location
        """

        # Try log blocks
        found, ppn = self.log_mapping_table.lpn_to_ppn(lpn)
        if found == True:
            return True, ppn, IN_LOG_BLOCK

        # Try data blocks
        found, ppn = self.data_block_mapping_table.lpn_to_ppn(lpn)
        if found == True:
            return True, ppn, IN_DATA_BLOCK

        # Cannot find lpn
        return False, None, None

class LogMappingTable(MappingBase):
    def __init__(self, confobj, block_pool, recorderobj, global_helper_obj):
        super(LogMappingTable, self).__init__(confobj, recorderobj,
            global_helper_obj)

        self.block_pool = block_pool

        # dgn -> log block info of data group (LogGroup2)
        self.log_group_info = {}

    def next_ppns_to_program(self, dgn, n, strip_unit_size):
        loggroup = self.log_group_info.setdefault(dgn,
            LogGroup2(self.conf, self.block_pool,
                max_n_log_blocks=self.conf['nkftl']['max_blocks_in_log_group']))
        return loggroup.next_ppns(n, strip_unit_size=strip_unit_size)

    def add_mapping(self, lpn, ppn):
        dgn = self.conf.nkftl_data_group_number_of_lpn(lpn)
        self.log_group_info[dgn].add_mapping(lpn, ppn)

    def remove_lpn(self, lpn):
        dgn = self.conf.nkftl_data_group_number_of_lpn(lpn)
        self.log_group_info[dgn].remove_lpn(lpn)

    def clear_data_group_info(self, dgn):
        self.log_group_info[dgn].clear()

    def lpn_to_ppn(self, lpn):
        """
        Translate lpn to ppn using log block info. Even the ppn does not
        exist here. It may exist in data blocks.

        Return: found, ppn/None
        """
        dgn = self.conf.nkftl_data_group_number_of_lpn(lpn)
        log_group_info = self.log_group_info.get(dgn, None)
        if log_group_info == None:
            return False, None
        return log_group_info.lpn_to_ppn(lpn)

    def remove_log_block(self, data_group_no, log_pbn):
        """
        It completely removes mapping info of log block log_lbn.
        For example, this can be used after switch merging log_lbn.
        """
        self.log_group_info[data_group_no].remove_log_block(log_pbn)


class GcDecider(object):
    def __init__(self, confobj, block_pool, recorderobj):
        self.conf = confobj
        self.block_pool = block_pool
        self.recorder = recorderobj

        self.high_watermark = self.conf['nkftl']['GC_threshold_ratio'] * \
            self.conf.n_blocks_per_dev
        self.low_watermark = self.conf['nkftl']['GC_low_threshold_ratio'] * \
            self.conf.n_blocks_per_dev
        # self.low_watermark = max( self.conf['nkftl']['GC_low_threshold_ratio'] * \
            # self.conf.n_blocks_per_dev,
            # self.conf.n_blocks_per_dev / self.conf['nkftl']['provision_ratio'])

        assert self.high_watermark > self.low_watermark

    def should_start(self):
        n_used_blocks = self.block_pool.total_used_blocks()

        return n_used_blocks > self.high_watermark

    def should_stop(self):
        n_used_blocks = self.block_pool.total_used_blocks()

        if self._freezed_too_long(n_used_blocks):
            return True
        else:
            # Is it higher than low watermark?
            return n_used_blocks < self.low_watermark

    def _improved(self, cur_n_used_blocks):
        """
        wether we get some free blocks since last call of this function
        """
        if self.last_used_blocks == None:
            ret = True
        else:
            # common case
            ret = cur_n_used_blocks < self.last_used_blocks

        self.last_used_blocks = cur_n_used_blocks
        return ret

    def _freezed_too_long(self, cur_n_used_blocks):
        return False

        if self._improved(cur_n_used_blocks):
            self.freeze_count = 0
            ret = False
        else:
            self.freeze_count += 1

            if self.freeze_count > 2 * self.conf.n_pages_per_block:
                ret = True
            else:
                ret = False

        return ret


class BlockInfo(object):
    """
    This is for sorting blocks to clean the victim.
    """
    def __init__(self, block_type, block_num, last_used_time,
            valid_ratio, data_group_no = None):
        self.valid_ratio = valid_ratio
        self.block_type = block_type
        self.block_num = block_num
        self.last_used_time = last_used_time
        self.data_group_no = data_group_no

    def __cmp__(self, other):
        """
        Low number will be retrieved first in priority queue
        """
        return cmp(self.last_used_time, other.last_used_time)


class VictimBlocksBase(object):
    def __init__(self, conf, block_pool, oob, rec, log_mapping_table,
            data_block_mapping_table):
        self.conf = conf
        self.block_pool = block_pool
        self.oob = oob
        self.rec = rec
        self.priority_q = Queue.PriorityQueue()
        self.log_mapping = log_mapping_table
        self.data_mapping = data_block_mapping_table

        self._init()

    def __iter__(self):
        while not self.priority_q.empty():
            b_info = self.priority_q.get()
            yield b_info

    def __len__(self):
        return self.priority_q.qsize()


class VictimDataBlocks(VictimBlocksBase):
    def _init(self):
        for blocknum in self.block_pool.data_usedblocks:
            if not self.oob.is_any_page_valid(blocknum):
                # no page is valid
                # we can only clean data without valid pages
                blk_info = BlockInfo(
                    block_type = TYPE_DATA_BLOCK,
                    block_num = blocknum,
                    valid_ratio = self.oob.states.block_valid_ratio(blocknum),
                    last_used_time = -1)  # high priority
                self.priority_q.put(blk_info)


class VictimLogBlocks(VictimBlocksBase):
    def _init(self):
        """
        TODO: is there any log blocks that are not in log mapping but in
        log_usedblocks?
        """
        for data_group_no, log_group_info in self.log_mapping.log_group_info.items():
            for curblock in log_group_info.cur_blocks():
                blk_info = BlockInfo(
                    block_type = TYPE_LOG_BLOCK,
                    block_num = curblock.blocknum,
                    valid_ratio = self.oob.states.block_valid_ratio(
                        curblock.blocknum),
                    last_used_time = 0,
                    data_group_no = data_group_no)
                self.priority_q.put(blk_info)


class GarbageCollector(object):
    def __init__(self, confobj, block_pool, flashobj, oobobj, recorderobj,
            translatorobj, global_helper_obj, log_mapping, data_block_mapping,
            simpy_env, des_flash, region_locks):
        self.conf = confobj
        self.flash = flashobj
        self.oob = oobobj
        self.block_pool = block_pool
        self.recorder = recorderobj
        self.translator = translatorobj
        self.log_mapping_table = log_mapping
        self.data_block_mapping_table = data_block_mapping
        self.env = simpy_env
        self.des_flash = des_flash
        self.region_locks = region_locks

        self.decider = GcDecider(self.conf, self.block_pool, self.recorder)

    def try_gc(self):
        triggered = False

        if self.decider.should_start() is False:
            return

        block_iter = self.victim_blocks_iter()

        while self.decider.should_stop() is False:
            try:
                blockinfo = block_iter.next()
            except StopIteration:
                print 'Out of victim blocks'
                self.recorder.count_me("GC", "StopIteration")
                break

            yield self.env.process(
                    self.clean_block(blockinfo, tag = TAG_THRESHOLD_GC))

    def victim_blocks_iter(self):
        return itertools.chain.from_iterable(
                [VictimDataBlocks(conf=self.conf,
                    block_pool=self.block_pool,
                    oob=self.oob,
                    rec=self.recorder,
                    log_mapping_table=self.log_mapping_table,
                    data_block_mapping_table=self.data_block_mapping_table),
                VictimLogBlocks(conf=self.conf,
                    block_pool=self.block_pool,
                    oob=self.oob,
                    rec=self.recorder,
                    log_mapping_table=self.log_mapping_table,
                    data_block_mapping_table=self.data_block_mapping_table)
                ])

    def clean_block(self, blk_info, tag):
        """
        Cleans one pariticular block, either data or log block. It will
        update all the relevant information such as mapping, oob, flash.
        """
        if blk_info.block_type == TYPE_DATA_BLOCK:
            yield self.env.process(
                self.recycle_empty_data_block(blk_info.block_num, tag))
        elif blk_info.block_type == TYPE_LOG_BLOCK:
            yield self.env.process(
                self.clean_log_block(blk_info.block_num, blk_info.data_group_no,
                    tag))

    def clean_log_block(self, log_pbn, data_group_no, tag):
        """
        0. If not valid page in log_pbn, simply erase and free it
        1. Try switch merge
        2. Try copy merge
        3. Try full merge
        """
        if log_pbn not in self.block_pool.log_usedblocks:
            # it is quite dynamic, this log block may have been
            # GCed with previous blocks
            return

        # Check to see if it is really the log block of the speicfed data
        # group
        if log_pbn not in self.translator.log_mapping_table\
                .log_group_info[data_group_no].log_block_numbers():
            # TODO: maybe you should just return here instead of panic?
            raise RuntimeError("{} is not a log block of data group {}"\
                .format(log_pbn, data_group_no))

        # Just free it?
        if not self.oob.is_any_page_valid(log_pbn):
            yield self.env.process(self._recycle_empty_log_block(
                data_group_no, log_pbn, tag))
            return

        is_mergable, logical_block = self.is_switch_mergable(log_pbn)
        # print 'switch merge  is_mergable:', is_mergable, 'logical_block:', logical_block
        if is_mergable == True:
            yield self.env.process(
                    self.switch_merge(log_pbn = log_pbn,
                    logical_block = logical_block))
            return

        is_mergable, logical_block, offset = self.is_partial_mergable(
            log_pbn)
        # print 'partial merge  is_mergable:', is_mergable, 'logical_block:', logical_block
        if is_mergable == True:
            yield self.env.process(
                self.partial_merge(log_pbn = log_pbn, lbn = logical_block,
                first_free_offset = offset))
            return

        yield self.env.process(self.full_merge(log_pbn))

    def clean_data_group(self, data_group_no):
        """
        This function will merge the contents of all log blocks associated
        with data_group_no into data blocks. After calling this function,
        there should be no log blocks remaining for this data group.

        TODO: Maybe also try to free empty data blocks here?
        """
        # We make local copy since we may need to modify the original data
        # in the loop
        # TODO: You need to GC the log blocks in a better order. This matters
        # because for example the first block may require full merge and the
        # second can be partial merged. Doing the full merge first may change
        # the states of the second log block and makes full merge impossible.
        log_block_list = copy.copy(self.log_mapping_table\
                .log_group_info[data_group_no].log_block_numbers())
        for log_block in log_block_list:
            # A log block may not be a log block anymore after the loop starts
            # It may be freed, it may be a data block now,.. Be careful
            yield self.env.process(
                    self.clean_log_block(
                        log_pbn=log_block,
                        data_group_no=data_group_no,
                        tag=TAG_WRITE_DRIVEN))
            self.asserts()

    def full_merge(self, log_pbn):
        """
        This log block (log_pbn) could contain pages from many different
        logical blocks in the same data group. For each logical block we
        find in this log block, we iterate all LPNs to and copy their
        data to a new free block.
        """
        self.recorder.count_me("garbage_collection", 'full_merge')

        # Find all the logical blocks
        ppn_start, ppn_end = self.conf.block_to_page_range(log_pbn)
        logical_blocks = set()
        for ppn in range(ppn_start, ppn_end):
            is_valid = self.oob.states.is_page_valid(ppn)
            # print 'ppn {} is valid: {}'.format(ppn, is_valid)
            if is_valid == True:
                lpn = self.oob.ppn_to_lpn[ppn]
                logical_block, _ = self.conf.page_to_block_off(lpn)
                logical_blocks.add(logical_block)

        # Move all the pages of a logical block to new block
        for logical_block in logical_blocks:
            yield self.env.process(
                self.aggregate_logical_block(logical_block, TAG_FULL_MERGE))
            self.asserts()

    def aggregate_logical_block(self, lbn, tag):
        """
        This function gathers all the logical pages in lbn
        and put them to a new physical block.

        The input logical block should have at least one valid page.
        Otherwise we will create a block with no valid pages.

        The procedure:
        1. for each lpn, if it is valid:
        2. copy content to ppn of the same offset in the new block
        3. invalidate the old ppn, validate the new ppn, update oob.ppn_to_lpn
        4. check old ppn's flash block, old_block
        5. if old_block is data block
        6. see if old_block has any valid page left, if not, erase the
        old flash block and free it. Also delete the data block mapping
        to old_block
        7. if old_block is a log block
        8. see if old_block has any valid page left, if not, erase the
        old flash block and free it. Also delete the mapping in log group info

        Well, you need to also consider the case that lbn has no valid pages,
        in which case you need to specifically remove the data block mapping
        """
        # We have to make sure this does not fail, somehow.

        req = self.region_locks.get_request(lbn)
        yield req

        # TODO: we should stop aggregating this logical block if all its
        # data are already in data block (in other words, log mapping has
        # no mapping for this logical block), because this means the data
        # has been already merged.
        # It does not hurt consistency even if
        # we do aggregate the logical block because locking the logical space
        # (logical block) means locking the corresponding physical space. And
        # If we have locked the logical block, every thing should be fine (unless
        # someone else is modifying without lock).
        if self._is_any_lpn_in_logmapping(lbn) is False:
            # nothing to merge now
            return

        self.asserts()
        dst_phy_block_num = self.block_pool.pop_a_free_block_to_data_blocks()

        lpn_start, lpn_end = self.conf.block_to_page_range(lbn)
        for lpn in range(lpn_start, lpn_end):
            in_block_page_off = lpn - lpn_start
            dst_ppn = self.conf.block_off_to_page(dst_phy_block_num,
                in_block_page_off)
            data_group_no = self.conf.nkftl_data_group_number_of_lpn(lpn)

            found, src_ppn, loc = self.translator.lpn_to_ppn(lpn)
            if found == True and self.oob.states.is_page_valid(src_ppn):
                data = self.flash.page_read(src_ppn, cat = tag)
                yield self.env.process(
                    self.des_flash.rw_ppns([src_ppn], 'read', tag = tag))
                self.flash.page_write(dst_ppn, cat = tag, data = data)
                yield self.env.process(
                    self.des_flash.rw_ppns([dst_ppn], 'write', tag = tag))

                self.oob.remap(lpn = lpn, old_ppn = src_ppn,
                    new_ppn = dst_ppn)

                # Now you've moved lpn, you need to remove lpn mapping if it is
                # in log blocks
                if loc == IN_LOG_BLOCK:
                    self.translator.log_mapping_table.remove_lpn(lpn)

                # After moving, you need to check if the source block of src_ppn
                # is totally free. If it is, we have to erase it and put it to
                # free block pool
                src_pbn, _ = self.conf.page_to_block_off(src_ppn)
                if not self.oob.is_any_page_valid(src_pbn):
                    # No page is valid
                    # We erase and free src_pbn, and clean up its mappings
                    self.oob.erase_block(src_pbn)
                    self.flash.block_erase(src_pbn, cat = tag)
                    yield self.env.process(
                        self.des_flash.erase_pbn_extent(src_pbn, 1, tag=tag))
                    if loc == IN_DATA_BLOCK:
                        self.block_pool.free_used_data_block(src_pbn)
                        lbn, _ = self.conf.page_to_block_off(lpn)
                        self.translator.data_block_mapping_table\
                            .remove_data_block_mapping(lbn = lbn)
                    elif loc == IN_LOG_BLOCK:
                        self.block_pool.free_used_log_block(src_pbn)
                        self.translator.log_mapping_table\
                            .remove_log_block(data_group_no = data_group_no,
                            log_pbn = src_pbn)
            else:
                # This lpn does not exist, so we just invalidate the
                # destination page. We have to do this because we can only
                # program flash sequentially.
                # self.flash.page_write(dst_ppn, tag, data = -1)
                self.oob.states.invalidate_page(dst_ppn)

        # Now we have all the pages in new block, we make the new block
        # the data block for lbn
        found, old_pbn = self.translator.data_block_mapping_table\
            .lbn_to_pbn(lbn)
        if found == True:
            # old_pbn must not have any valid pages, so we free it
            yield self.env.process(
                    self.recycle_empty_data_block(old_pbn, tag = tag))
        self.translator.data_block_mapping_table.add_data_block_mapping(
            lbn = lbn, pbn = dst_phy_block_num)

        self.region_locks.release_request(lbn, req)

    def _is_any_lpn_in_logmapping(self, lbn):
        start, end = self.conf.block_to_page_range(lbn)
        for lpn in range(start, end):
            found, _ = self.log_mapping_table.lpn_to_ppn(lpn)
            if found is True:
                return True
        return False

    def is_partial_mergable(self, log_pbn):
        """
        This function tells if log_pbn is partial mergable.

        To be partial mergable, you need:
        1. first k pages are valid, the rest are erased
        2. the first k pages are aligned with logical block
        3. the kth-nth pages can exist in data block, other log blocks or not
        exist

        TODO: we need to also handle the case of half the block is valid
        and aligned, but the rest are invalid. The rest of the dat of this
        logical block does not exist anywhere. So we don't need to copy
        data to the physical block.

        TODO: we need to handle the case of first half of data block is valid
        and the rest is erased, in which case we can copy the data from log
        block to data block.

        Return: True/False, logical block, offset of the first erased page
        """
        ppn_start, ppn_end = self.conf.block_to_page_range(log_pbn)

        # first k pages must be valid
        last_valid_ppn = None
        for ppn in range(ppn_start, ppn_end):
            if self.oob.states.is_page_valid(ppn):
                last_valid_ppn = ppn
            else:
                break

        if last_valid_ppn == None or last_valid_ppn == ppn_end - 1:
            # nobody is valid or everybody is valid
            # if everybody is valid, it could be switch mergable, but not
            # partial mergable.
            return False, None, None

        # the rest must be erased
        for ppn in range(last_valid_ppn + 1, ppn_end):
            if not self.oob.states.is_page_erased(ppn):
                return False, None, None

        # the valid pages must be aligned between logical and physical address
        lpn_start = self.oob.ppn_to_lpn[ppn_start]
        logical_block, off = self.conf.page_to_block_off(lpn_start)
        if off != 0:
            # the first lpn is not aligned
            return False, None, None

        for ppn in range(ppn_start, last_valid_ppn + 1):
            lpn = self.oob.ppn_to_lpn[ppn]
            if lpn - lpn_start != ppn - ppn_start:
                # not aligned
                return False, None, None

        # now we know it is parital mergable
        return True, logical_block, last_valid_ppn + 1 - ppn_start

    def partial_merge(self, log_pbn, lbn, first_free_offset):
        """
        The lpn in the kth-nth pages:
        if not exist:
            invalidate the corresponding page
        if in data block:
            copy it to the corresponding page
            check if we need to free the data block
        if in log block:
            copy it to the corresponding page
            check if we need to free the log block

        After this function, log_pbn will become a data block
        """
        # TODO: should we call partial merge by logical block number, in
        # stead of physical block number? Easier to reason about the locks
        req = self.region_locks.get_request(lbn)
        yield req

        is_mergable, logical_block_ret, offset_ret = self.is_partial_mergable(
                log_pbn)
        if is_mergable is False or logical_block_ret != lbn or \
                offset_ret != first_free_offset:
            # we need to double check here since while we wait, things may
            # have changed. We check logical_block_ret != logical_block
            # because we only lock logical block, it is possible that
            # two switch_merge for the same log block run in parallel.
            return

        self.recorder.count_me("garbage_collection", 'partial_merge')

        data_group_no = self.conf.nkftl_data_group_number_of_logical_block(
            lbn)
        # Copy
        for offset in range(first_free_offset,
                self.conf.n_pages_per_block):
            lpn = self.conf.block_off_to_page(lbn, offset)
            dst_ppn = self.conf.block_off_to_page(log_pbn, offset)

            found, src_ppn, location = self.translator.lpn_to_ppn(lpn)
            if not found or not self.oob.states.is_page_valid(src_ppn):
                # the lpn does not exist
                # 1. cannot find
                # 2. found, but not valid
                self.oob.states.invalidate_page(dst_ppn)
            elif found == True and location == IN_DATA_BLOCK:
                src_block, _ = self.conf.page_to_block_off(src_ppn)
                data = self.flash.page_read(src_ppn, cat = TAG_PARTIAL_MERGE)
                yield self.env.process(
                    self.des_flash.rw_ppns([src_ppn], 'read', tag=TAG_PARTIAL_MERGE))
                self.flash.page_write(dst_ppn, cat = TAG_PARTIAL_MERGE,
                    data = data)
                yield self.env.process(
                    self.des_flash.rw_ppns([dst_ppn], 'write', tag=TAG_PARTIAL_MERGE))
                self.oob.remap(lpn, old_ppn = src_ppn, new_ppn = dst_ppn)

                # This branch may never be called because the none of the rest
                # of the pages is valid, thus you don't have the change
                # to recyle the old data block
                # for example:
                #         - Log BLock -
                #          pages: 0123
                #          state: VEEE
                #  logical block:   7
                #
                #         - Data BLock -
                #          pages: 0123
                #          state: IIII
                #  logical block:   7
                # In the case above, log block is partial mergable. When you
                # try to partial merge it, you try to find valid pages of page
                # 123. But none of them is valid. So you will not come to this
                # branch. So the data block will not be recyled.
                #
                # Actually it is not a good idea to recyle the data block here.
                # Because there should be only one data block associated with
                # on partial merge. You can just find out which one it is and
                # try to recycle it once.
                # self.recycle_empty_data_block(src_block,
                    # tag = TAG_PARTIAL_MERGE ) # check and then recycle
            elif found == True and location == IN_LOG_BLOCK:
                src_block, _ = self.conf.page_to_block_off(src_ppn)
                # If the lpn is in log block
                data = self.flash.page_read(src_ppn, cat = TAG_PARTIAL_MERGE)
                yield self.env.process(
                    self.des_flash.rw_ppns([src_ppn], 'read', tag = TAG_PARTIAL_MERGE))
                self.flash.page_write(dst_ppn, cat = TAG_PARTIAL_MERGE,
                    data = data)
                yield self.env.process(
                    self.des_flash.rw_ppns([dst_ppn], 'write', tag = TAG_PARTIAL_MERGE))
                self.oob.remap(lpn, old_ppn = src_ppn, new_ppn = dst_ppn)

                # you need to remove lpn from log mapping here
                self.translator.log_mapping_table.remove_lpn(lpn=lpn)

                yield self.env.process(
                    self._recycle_empty_log_block(data_group_no = data_group_no,
                    log_pbn = src_block, tag = TAG_PARTIAL_MERGE))

        # If there is an old data block, we need to recycle it because we
        # now have a new one.
        found, old_pbn = self.translator.data_block_mapping_table\
            .lbn_to_pbn(lbn = lbn)
        if found:
            yield self.env.process(
                self.recycle_empty_data_block(old_pbn, tag = TAG_PARTIAL_MERGE))

        # Now the log block lgo_pbn has all the content of lbn
        # Now add the new mapping
        self.translator.data_block_mapping_table\
            .add_data_block_mapping(lbn = lbn, pbn = log_pbn)

        # Remove log_pbn from log group
        self.translator.log_mapping_table.remove_log_block(
            data_group_no = data_group_no, log_pbn = log_pbn)

        # move from log pool to data pool
        self.block_pool.move_used_log_to_data_block(log_pbn)

        self.region_locks.release_request(lbn, req)

    def is_switch_mergable(self, log_pbn):
        """
        To be switch mergable, the block has to satisfy the following
        conditions:
        1. all pages are valid
        2. all LPNs are 'aligned' with block page numbers

        It also returns the corresponding logical block number if it is
        switch mergable.

        Return Mergable?, logical_block
        """
        ppn_start, ppn_end = self.conf.block_to_page_range(log_pbn)
        lpn_start = None
        logical_block = None
        for ppn in range(ppn_start, ppn_end):
            if not self.oob.states.is_page_valid(ppn):
                return False, None
            lpn = self.oob.ppn_to_lpn[ppn]
            if lpn_start == None:
                logical_block, logical_off = self.conf.page_to_block_off(lpn)
                if logical_off != 0:
                    return False, None
                lpn_start = lpn
                # Now we know at least the lpn_start and ppn_start are aligned
                continue
            if lpn - lpn_start != ppn - ppn_start:
                return False, None

        return True, logical_block

    def switch_merge(self, log_pbn, logical_block):
        """
        Merge log_pbn, which corresponds to logical_block

        1. Before calling this function, make sure log_pbn is switch
        mergable
        2. Find and erase the old physical block corresponding to the logical
        block in Data Block Mapping Table (if there is one), put it to free
        block pool. Update the mapping logical block -> log_pbn
        3. Update log group info:
             remove log_pbn from log_blocks. This is SingleLogBlockInfo
             remove all page mappings in page_map
             set _cur_log_block to None
        """

        req = self.region_locks.get_request(logical_block)
        yield req

        is_mergable, logical_block_ret = self.is_switch_mergable(log_pbn)
        if is_mergable is False or logical_block_ret != logical_block:
            # we need to double check here since while we wait, things may
            # have changed. We check logical_block_ret != logical_block
            # because we only lock logical block, it is possible that
            # two switch_merge for the same log block run in parallel.
            return

        self.recorder.count_me("garbage_collection", 'switch_merge')

        # erase old data block
        found, old_physical_block = self.translator.data_block_mapping_table\
            .lbn_to_pbn(logical_block)

        if found:
            # clean up old_physical_block
            self.oob.erase_block(old_physical_block)
            self.flash.block_erase(old_physical_block, cat = TAG_SWITCH_MERGE)
            yield self.env.process(
                self.des_flash.erase_pbn_extent(old_physical_block, 1,
                    tag=TAG_SWITCH_MERGE))
            self.block_pool.free_used_data_block(old_physical_block)
            # self.translator.data_block_mapping_table.remove_mapping(
                # logical_block)

        # update data block mapping table
        # This will override the old mapping if there is one
        self.translator.data_block_mapping_table.add_data_block_mapping(
            logical_block, log_pbn)

        # Update log mapping table
        data_group_no = self.conf.nkftl_data_group_number_of_logical_block(
                logical_block)
        self.translator.log_mapping_table.remove_log_block(
                data_group_no = data_group_no,
                log_pbn = log_pbn)
        # Need to mark the log block as used data block now
        self.block_pool.move_used_log_to_data_block(log_pbn)

        self.region_locks.release_request(logical_block, req)

    def recycle_empty_data_block(self, data_block, tag):
        if data_block in self.block_pool.data_usedblocks and \
            not self.oob.is_any_page_valid(data_block):
            self.oob.erase_block(data_block)
            self.flash.block_erase(data_block, cat = tag)
            yield self.env.process(
                self.des_flash.erase_pbn_extent(data_block, 1, tag=tag))
            # need to remove data block mapping
            self.translator.data_block_mapping_table\
                .remove_data_block_mapping_by_pbn(data_block)
            self.block_pool.free_used_data_block(data_block)

    def _recycle_empty_log_block(self, data_group_no, log_pbn, tag):
        """
        We will double check to see if the block has any valid page
        Remove the log block in every relevant data structure
        """
        if log_pbn in self.block_pool.log_usedblocks and \
            not self.oob.is_any_page_valid(log_pbn):

            self.oob.erase_block(log_pbn)

            self.flash.block_erase(log_pbn, cat = tag)
            yield self.env.process(
                self.des_flash.erase_pbn_extent(log_pbn, 1, tag=tag))

            self.block_pool.free_used_log_block(log_pbn)
            # remove log mapping
            self.translator.log_mapping_table.remove_log_block(
                data_group_no = data_group_no,
                log_pbn = log_pbn)

    def assert_mapping(self, lpn):
        # Try log blocks
        log_found, log_ppn = self.translator.log_mapping_table.lpn_to_ppn(lpn)

        # Try data blocks
        data_found, data_ppn = self.translator.data_block_mapping_table\
            .lpn_to_ppn(lpn)

        if log_found == True and self.oob.states.is_page_valid(log_ppn) and \
            data_found == True and self.oob.states.is_page_valid(data_ppn):
            raise RuntimeError("lpn:{} is valid in log mapping (ppn:{}) "\
                "and data mapping (ppn:{})".format(lpn, log_ppn, data_ppn))

    def asserts(self):
        # number of log blocks in logblockinfo should be equal to
        # used log blocks in block_pool
        log_block_cnt = 0
        for dgn, loggroupinfo in self.translator.log_mapping_table\
            .log_group_info.items():
            logblocks = loggroupinfo.log_block_numbers()
            log_block_cnt += len(logblocks)
            assert len(logblocks) <= self.conf['nkftl']['max_blocks_in_log_group']

        if not log_block_cnt == len(self.block_pool.log_usedblocks):
            raise RuntimeError(
                "not log_block_cnt{} == len(self.block_pool.log_usedblocks{})"\
                .format(log_block_cnt, len(self.block_pool.log_usedblocks)))


        # number of data blocks in data_block_mapping_table should be equal to
        # used data blocks in block_pool
        # NEW WITH CONCURRENCY: This may not be true when a transaction of
        # full merge, or others, is going on. An asserts() check could be
        # triggered when a transaction is going on.
        # data_blocks_in_map = len(self.translator.data_block_mapping_table\
            # .logical_to_physical_block)
        # if not data_blocks_in_map == len(self.block_pool.data_usedblocks):
            # raise RuntimeError(
                # "not data_blocks_in_map ({}) == len(self.block_pool.data_usedblocks) ({})"\
                # .format(data_blocks_in_map, len(self.block_pool.data_usedblocks)))


class Ftl(ftlbuilder.FtlBuilder):
    """
    This is an FTL implemented according to paper:
        A reconfigurable FTL Architecture for NAND Flash-Based Applications
    """
    def __init__(self, confobj, recorderobj, flashobj, simpy_env, des_flash):
        super(Ftl, self).__init__(confobj, recorderobj, flashobj)

        self.des_flash = des_flash
        self.env = simpy_env
        self.block_pool = NKBlockPool(
            n_channels=self.conf.n_channels_per_dev,
            n_blocks_per_channel=self.conf.n_blocks_per_channel,
            n_pages_per_block=self.conf.n_pages_per_block,
            tags=[TDATA, TLOG])
        self.oob = OutOfBandAreas(confobj)
        self.global_helper = GlobalHelper(confobj)

        self.data_block_mapping_table = DataBlockMappingTable(confobj,
            recorderobj, self.global_helper)

        self.log_mapping_table = LogMappingTable(confobj,
            self.block_pool, recorderobj, self.global_helper)

        self.region_locks = LockPool(self.env)

        ###### the managers ######
        self.translator = Translator(
            confobj = self.conf,
            recorderobj = recorderobj,
            global_helper_obj = self.global_helper,
            log_mapping = self.log_mapping_table,
            data_block_mapping = self.data_block_mapping_table
            )

        # Garbage collector is considered to be in the highest level
        self.garbage_collector = GarbageCollector(
            confobj = self.conf,
            block_pool = self.block_pool,
            flashobj = self.flash,
            oobobj = self.oob,
            recorderobj = recorderobj,
            translatorobj = self.translator,
            global_helper_obj = self.global_helper,
            log_mapping = self.log_mapping_table,
            data_block_mapping = self.data_block_mapping_table,
            simpy_env = self.env,
            des_flash = self.des_flash,
            region_locks = self.region_locks
            )

    def lpn_to_ppn(self, lpn):
        found, ppn, location = self.translator.lpn_to_ppn(lpn)
        if found == True and self.oob.states.is_page_valid(ppn):
            return found, ppn, location
        return False, None, None




    def lba_read(self, lpn):
        content = yield self.env.process(self.read_ext(Extent(lpn, 1)))
        self.env.exit(content[0])

    def read_ext(self, extent):
        extents = split_ext_by_region(self.conf['n_pages_per_region'], extent)
        ext_data = []
        for region_ext in extents:
            ret_data = yield self.env.process(self.read_region(region_ext))
            ext_data.extend(ret_data)

        self.env.exit(ext_data)

    def read_region(self, extent):
        region_id = self.conf.region_id_of_lpn(extent.lpn_start)
        req = self.region_locks.get_request(region_id)
        yield req

        ppns_to_read = []
        contents = []
        for lpn in extent.lpn_iter():
            hasit, ppn, loc = self.lpn_to_ppn(lpn)

            if hasit == True:
                content = self.flash.page_read(ppn, cat = TAG_FORGROUND)
                ppns_to_read.append(ppn)
            else:
                content = None
            contents.append(content)

        yield self.env.process(
            self.des_flash.rw_ppns(ppns_to_read, 'read', tag = "Unknown"))

        self.region_locks.release_request(region_id, req)

        self.env.exit(contents)




    def lba_write(self, lpn, data=None):
        yield self.env.process(
                self.write_ext(Extent(lpn_start=lpn, lpn_count=1), [data]))

        yield self.env.process(self.garbage_collector.try_gc())

    def write_ext(self, extent, data=None):
        extents = split_ext_by_region(self.conf.n_pages_per_region(), extent)
        for region_ext in extents:
            if data is None:
                region_data = None
            else:
                region_data = self._sub_ext_data(data, extent, region_ext)
            yield self.env.process(
                    self.write_region(region_ext, data=region_data))

    def write_region(self, extent, data=None):
        """
        lpns in extent must be in the same region
        a region must in the same data group
        """
        region_id = self.conf.region_id_of_lpn(extent.lpn_start)
        req = self.region_locks.get_request(region_id)
        yield req

        data_group_no = self.conf.nkftl_data_group_number_of_lpn(extent.lpn_start)
        for lpn in extent.lpn_iter():
            assert self.conf.nkftl_data_group_number_of_lpn(lpn) == data_group_no

        loop_ext = copy.copy(extent)
        while loop_ext.lpn_count > 0:
            ppns = self.log_mapping_table.next_ppns_to_program(
                dgn=data_group_no,
                n=loop_ext.lpn_count,
                strip_unit_size=self.conf['stripe_size'])

            n_ppns = len(ppns)
            mappings = dict(zip(loop_ext.lpn_iter(), ppns))
            assert len(mappings) == n_ppns
            if data is None:
                loop_data = None
            else:
                loop_data = self._sub_ext_data(data, extent, loop_ext)
            yield self.env.process(
                    self._write_log_ppns(mappings, data=loop_data))

            if n_ppns < loop_ext.lpn_count:
                # we cannot find vailable pages in log blocks
                self.region_locks.release_request(region_id, req)

                yield self.env.process(
                    self.garbage_collector.clean_data_group(data_group_no))

                req = self.region_locks.get_request(region_id)
                yield req

            loop_ext.lpn_start += n_ppns
            loop_ext.lpn_count -= n_ppns

        self.region_locks.release_request(region_id, req)

        # TODO: maybe this should not be here.
        # yield self.env.process(self.garbage_collector.try_gc())

    def _sub_ext_data(self, data, extent, sub_ext):
        start = sub_ext.lpn_start - extent.lpn_start
        count = sub_ext.lpn_count
        sub_data = data[start:(start + count)]
        return sub_data

    def _write_log_ppns(self, mappings, data=None):
        """
        The ppns in mappings is obtained from loggroup.next_ppns()
        """
        # data block mapping
        pass

        # oob states,    invalidate old ppn, validate new ppn
        # oob ppn->lpn
        # must remap oob before _update_log_mappings because once we
        # update log mappings, we lose the old ppn
        self._remap_oob(mappings)

        # log mapping
        self._update_log_mappings(mappings)

        # block pool
        # no need to handle because it has been handled when we got the ppns

        # flash
        for i, ppn in enumerate(mappings.values()):
            if data is None:
                pagedata = None
            else:
                pagedata = data[i]
            self.flash.page_write(ppn, cat='', data=pagedata)

        # des flash
        yield self.env.process(
            self.des_flash.rw_ppns(mappings.values(), 'write',
                tag = "Unknown"))

    def _update_log_mappings(self, mappings):
        """
        The ppns in mappings must have been get by loggroup.next_ppns()
        """
        for lpn, ppn in mappings.items():
            self.log_mapping_table.add_mapping(lpn, ppn)

    def _remap_oob(self, new_mappings):
        for lpn, new_ppn in new_mappings.items():
            found, old_ppn, loc = self.translator.lpn_to_ppn(lpn)
            if found == False:
                old_ppn = None
            self.oob.remap(lpn = lpn, old_ppn = old_ppn, new_ppn = new_ppn)







    def lba_discard(self, lpn):
        yield self.env.process(self._discard_region(Extent(lpn, 1)))

    def discard_ext(self, extent):
        extents = split_ext_by_region(self.conf['n_pages_per_region'], extent)
        for region_ext in extents:
            yield self.env.process(self._discard_region(region_ext))

    def _discard_region(self, extent):
        region_id = self.conf.region_id_of_lpn(extent.lpn_start)
        req = self.region_locks.get_request(region_id)
        yield req

        for lpn in extent.lpn_iter():
            found, ppn, loc = self.translator.lpn_to_ppn(lpn)
            if found == True:
                if loc == IN_LOG_BLOCK:
                    self.translator.log_mapping_table.remove_lpn(lpn)
                self.oob.wipe_ppn(ppn)

        self.region_locks.release_request(region_id, req)

    def post_processing(self):
        pass


def split_ext_by_region(n_pages_per_region, extent):
    if extent.lpn_count == 0:
        return None

    last_seg_id = -1
    cur_ext = None
    exts = []
    for lpn in extent.lpn_iter():
        seg_id = lpn / n_pages_per_region
        if seg_id == last_seg_id:
            cur_ext.lpn_count += 1
        else:
            if cur_ext is not None:
                exts.append(cur_ext)
            cur_ext = Extent(lpn_start=lpn, lpn_count=1)
        last_seg_id = seg_id

    if cur_ext is not None:
        exts.append(cur_ext)

    return exts


