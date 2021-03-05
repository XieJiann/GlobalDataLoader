import multiprocessing
import queue
from multiprocessing import shared_memory
from mylog import *
import signal, os, sys
import mmap


class Buffer(object):
    def __init__(self, name, create=False, size=0):
        # self.shm = None
        if create:
            # path = "/tmp/"+name
            # f = open(path, "wb+")
            # f.write(b'0'*size)
            # self.buf = mmap.mmap(f.fileno(), size)
            # self.buf.flush()
            self.shm = shared_memory.SharedMemory(name, create, size)
            self.buf = self.shm.buf
        else:
            # path = "/tmp/"+name
            # f = open(path, "rb+")
            # self.buf = mmap.mmap(f.fileno(), size)
            self.shm = shared_memory.SharedMemory(name)
            self.buf = self.shm.buf
        # self in_queue = in_queue
        self.create = create
        self.size = size
        self.name = name

        # | -- inode -->     <-- data --|
        self.inode_tail = 0
        self.data_head = size

        self.task_tails = {}
        self.task_heads = {}
        self.data_refs = {}

        # basic config
        # | VALID BYTE | DATA_IDX | NEXT_IDX |
        self.INDEX_LEN = 4
        self.VALID_LEN = 1
        self.INODE_LEN = self.VALID_LEN + 2 * self.INDEX_LEN

        self.VALID_OFF = 0
        self.DATA_IDX_OFF = self.VALID_LEN
        self.NEXT_IDX_OFF = self.VALID_LEN + self.INDEX_LEN

        # data = | datasize | data |
        self.DATASIZE_LEN = 4
        self.BYTE_ORDER = 'big'
        self.DATA_LEN = -1

        # VALID BYTE state
        self.DATA_OK = 0x1
        self.NEXT_OK = 0x2
        self.USED = 0x4

    def read(self, idx):
        data_idx_byte = self.buf[idx + self.DATA_IDX_OFF:idx +
                                 self.DATA_IDX_OFF + self.INDEX_LEN]
        data_idx = int.from_bytes(data_idx_byte, self.BYTE_ORDER)

        datasize_byte = self.buf[data_idx:data_idx + self.DATASIZE_LEN]
        datasize = int.from_bytes(datasize_byte, self.BYTE_ORDER)

        data_byte = self.buf[data_idx + self.DATASIZE_LEN:data_idx +
                             self.DATASIZE_LEN + datasize]

        self.buf[idx + self.VALID_OFF] &= (~self.DATA_OK)
        logging.info("read data(%d) inode %d in %s", data_idx, idx, self.name)
        return data_byte.tobytes()

    def get_next(self, idx):
        if self.buf[idx + self.VALID_OFF] & self.NEXT_OK == 0:
            return -1
        next_idx_byte = self.buf[idx + self.NEXT_IDX_OFF:idx +
                                 self.NEXT_IDX_OFF + self.INDEX_LEN]
        next_idx = int.from_bytes(next_idx_byte, self.BYTE_ORDER)
        self.buf[idx + self.VALID_OFF] &= (~self.USED)
        return next_idx

    def add_task(self, task_name):
        if task_name in self.task_heads.keys():
            return -1
        inode_idx = self._allocate_inode()
        self.buf[inode_idx] |= self.USED

        self.task_heads[task_name] = inode_idx
        self.task_tails[task_name] = inode_idx
        return inode_idx

    def write(self, data, task_name_list):
        assert (type(data) == bytes)
        if self.DATA_LEN == -1:
            self.DATA_LEN = len(data) + self.DATASIZE_LEN

        data_idx = self._write_data(data)

        self.data_refs[data_idx] = []
        for task_name in task_name_list:
            if task_name not in self.task_tails.keys():
                return -1
            inode_idx = self._write_inode(self.task_tails[task_name], data_idx)

            logging.info("write data[%d] ref(%s) in (%d ->) %d in %s",
                         data_idx, task_name, self.task_tails[task_name],
                         inode_idx, self.name)
            self.task_tails[task_name] = inode_idx

            self.data_refs[data_idx].append(inode_idx)
        return data_idx

    def _write_inode(self, lastnode_idx, data_idx):
        curnode_idx = self._allocate_inode()
        self.buf[curnode_idx] |= self.USED

        curnode_idx_byte = curnode_idx.to_bytes(self.INDEX_LEN,
                                                self.BYTE_ORDER)
        lastnode_idx_byte = lastnode_idx.to_bytes(self.INDEX_LEN,
                                                  self.BYTE_ORDER)
        data_idx_byte = data_idx.to_bytes(self.INDEX_LEN, self.BYTE_ORDER)

        # copy this data idx
        self.buf[curnode_idx + self.DATA_IDX_OFF:curnode_idx +
                 self.DATA_IDX_OFF + self.INDEX_LEN] = data_idx_byte
        self.buf[curnode_idx + self.VALID_OFF] |= self.DATA_OK

        # link last idx
        self.buf[lastnode_idx + self.NEXT_IDX_OFF:lastnode_idx +
                 self.NEXT_IDX_OFF + self.INDEX_LEN] = curnode_idx_byte
        self.buf[lastnode_idx + self.VALID_OFF] |= self.NEXT_OK

        return curnode_idx

    def _write_data(self, data):
        data_idx = self._allocate_data()
        size_byte = len(data).to_bytes(self.DATASIZE_LEN,
                                       byteorder=self.BYTE_ORDER)

        # write data
        assert(len(size_byte+data) == self.DATA_LEN)
        self.buf[data_idx:data_idx + self.DATA_LEN] = size_byte + data

        return data_idx

    def _allocate_inode(self, ):
        if self.inode_tail + self.INODE_LEN < self.data_head:
            idx = self.inode_tail
            self.inode_tail += self.INODE_LEN
            self.buf[idx + self.VALID_OFF] &= 0
            return idx

        # 当找不到空闲的节点，一直轮询，是否合理？
        while True:
            # time.sleep(1)
            # print("find free inode")
            for key in self.task_heads.keys():
                idx = self.task_heads[key]
                if self.buf[idx] & self.USED == 0:
                    new_head = int.from_bytes(
                        self.buf[idx + self.NEXT_IDX_OFF:idx +
                                 self.NEXT_IDX_OFF + self.INDEX_LEN],
                        byteorder=self.BYTE_ORDER)
                    self.task_heads[key] = new_head
                    self.buf[idx + self.VALID_OFF] &= 0
                    return idx

    def _allocate_data(self, ):
        if self.data_head - self.DATA_LEN > self.inode_tail:
            self.data_head = self.data_head - self.DATA_LEN
            return self.data_head
        #一直轮询
        while True:
            # time.sleep(1)
            # print("find free data")
            for i in range(self.size - self.DATA_LEN, self.data_head - 1,
                           -self.DATA_LEN):
                free = True
                refs = self.data_refs[i]
                for ref in refs:
                    ref_data_idx = int.from_bytes(
                        self.buf[ref + self.DATA_IDX_OFF:ref +
                                 self.DATA_IDX_OFF + self.INDEX_LEN],
                        self.BYTE_ORDER)
                    if self.buf[
                            ref + self.
                            VALID_OFF] & self.DATA_OK != 0 and ref_data_idx == i:
                        free = False
                        break
                if free:
                    return i

    def delete_task(self, name):
        head = self.task_heads[name]
        while self.get_next(head) != -1:
            head = self.get_next(head)
            # print("delete")
        del self.task_heads[name]
        del self.task_tails[name]

    def debug_print(self, ):
        for i in range(0, self.inode_tail, self.INODE_LEN):
            print(self.buf[i],
                  int.from_bytes(self.buf[i + 1:i + 5], self.BYTE_ORDER),
                  int.from_bytes(self.buf[i + 5:i + 9], self.BYTE_ORDER),
                  end=' | ')

    def __del__(self, ):
        # pass
        self.shm.close()
        self.shm.unlink()


import threading
import time
n = 100


def writer(c):
    for i in range(n):
        d = c.write(str.encode(str(i%10)) * 602116, ["task1", "task2"])
        # print("write",i," in ", d)


def reader(node, c, name):
    time.sleep(5)
    for i in range(n):
        now = time.time()
        next_node = c.get_next(node)
        while next_node == -1:
            next_node = c.get_next(node)
            #time.sleep(0.1)
        data = c.read(next_node)
        t = time.time() - now
        print(name, "read", chr(data[0]), t)
        node = next_node
    c.delete_task(name)


def test():
    c = Buffer("xiejian", True, 602116 * 50)
    t1 = c.add_task("task1")
    t2 = c.add_task("task2")
    try:
        w = threading.Thread(target=writer, args=(c, ))
        r1 = threading.Thread(target=reader, args=(t1, c, "task1"))
        r2 = threading.Thread(target=reader, args=(t2, c, "task2"))

        w.start()
        r1.start()
        r2.start()

        r1.join()
        r2.join()
        w.join()
        c.debug_print()
    except:
        del c
        return


if __name__ == '__main__':

    def _signal_handler(signum, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    test()