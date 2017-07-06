import elit

tsv_file_path = '../resources/sample/sample.tsv'

reader = elit.TSVReader(1,2,3,4,5,6,7,8)

if __name__ == '__main__':
    reader.open(tsv_file_path)

    for i, node in enumerate(reader.next_all):
        print("Node: {}".format(i))
        for k, line in enumerate(node):
            print("{}".format(line))