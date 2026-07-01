# encoding: ascii-8bit

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See LICENSE.md for more details.

# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

require 'spec_helper'
require_relative '../lib/json_telemetry_server_interface'
require 'openc3/packets/packet'
require 'openc3/system/system'
require 'net/http'
require 'json'

module OpenC3
  TEST_PORT = 19765

  def self.make_test_packet(target_name, packet_name, items)
    pkt = Packet.new(target_name, packet_name)
    offset = 0
    items.each do |name, (bit_size, data_type)|
      item = PacketItem.new(name, offset, bit_size, data_type.to_sym, :BIG_ENDIAN)
      item.key = name
      pkt.define(item)
      offset += bit_size
    end
    pkt.restore_defaults
    pkt
  end

  def self.http_post(port, path, body, headers = {}, method: :Post)
    uri = URI("http://127.0.0.1:#{port}#{path}")
    http = Net::HTTP.new(uri.host, uri.port)
    klass = { Post: Net::HTTP::Post, Get: Net::HTTP::Get, Put: Net::HTTP::Put, Delete: Net::HTTP::Delete }[method]
    req = klass.new(uri.path)
    req['Content-Type'] = 'application/json'
    headers.each { |k, v| req[k] = v }
    req.body = body
    http.request(req)
  end

  describe JsonTelemetryServerInterface do
    let(:sensors_pkt) do
      OpenC3.make_test_packet('JSON_TLM_TEST', 'SENSORS', {
        'TEMPERATURE' => [32, 'FLOAT'],
        'PRESSURE'    => [32, 'FLOAT'],
        'COUNT'       => [32, 'UINT'],
        'STATUS'      => [128, 'STRING'],
      })
    end

    let(:health_pkt) do
      OpenC3.make_test_packet('JSON_TLM_TEST', 'HEALTH', { 'UPTIME' => [32, 'UINT'] })
    end

    before(:each) do
      setup_system()
      allow(System).to receive_message_chain(:telemetry, :packet) do |target, pkt_name|
        lookup = {
          ['JSON_TLM_TEST', 'SENSORS'] => sensors_pkt,
          ['JSON_TLM_TEST', 'HEALTH']  => health_pkt,
        }
        result = lookup[[target.upcase, pkt_name.upcase]]
        raise "Unknown packet #{target}/#{pkt_name}" unless result
        result
      end
    end

    after(:each) do
      kill_leftover_threads()
    end

    # ------------------------------------------------------------------
    # Unit: JSON-to-item mapping
    # ------------------------------------------------------------------

    describe '#map_json_to_packet' do
      let(:iface) { JsonTelemetryServerInterface.new(TEST_PORT + 1) }

      it 'maps matching keys case-insensitively' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'TEMPERATURE' => [32, 'FLOAT'] })
        iface.send(:map_json_to_packet, { 'temperature' => 25.5 }, pkt)
        expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(25.5)
      end

      it 'maps uppercase JSON key to uppercase item' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'TEMPERATURE' => [32, 'FLOAT'] })
        iface.send(:map_json_to_packet, { 'TEMPERATURE' => 30.0 }, pkt)
        expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(30.0)
      end

      it 'silently ignores unrecognized keys' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'TEMPERATURE' => [32, 'FLOAT'] })
        expect { iface.send(:map_json_to_packet, { 'TEMPERATURE' => 1.0, 'UNKNOWN' => 99 }, pkt) }.not_to raise_error
      end

      it 'leaves unmentioned items at their defaults' do
        pkt = OpenC3.make_test_packet('T', 'P', {
          'TEMPERATURE' => [32, 'FLOAT'],
          'COUNT'       => [32, 'UINT'],
        })
        pkt.restore_defaults
        iface.send(:map_json_to_packet, { 'TEMPERATURE' => 5.0 }, pkt)
        expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(5.0)
        expect(pkt.read('COUNT')).to eq(0)
      end

      it 'coerces integer JSON value to FLOAT item' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'TEMPERATURE' => [32, 'FLOAT'] })
        iface.send(:map_json_to_packet, { 'TEMPERATURE' => 30 }, pkt)
        expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(30.0)
      end

      it 'writes string value to STRING item' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'STATUS' => [128, 'STRING'] })
        iface.send(:map_json_to_packet, { 'STATUS' => 'OK' }, pkt)
        expect(pkt.read('STATUS').strip.delete("\x00")).to eq('OK')
      end

      it 'handles all case variants' do
        pkt = OpenC3.make_test_packet('T', 'P', { 'TEMPERATURE' => [32, 'FLOAT'] })
        %w[temperature Temperature TEMPERATURE tEmPeRaTuRe].each do |key|
          pkt.restore_defaults
          iface.send(:map_json_to_packet, { key => 1.0 }, pkt)
          expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(1.0), "failed for key=#{key}"
        end
      end
    end

    # ------------------------------------------------------------------
    # HTTP contract
    # ------------------------------------------------------------------

    describe 'HTTP contract' do
      let(:iface) { JsonTelemetryServerInterface.new(TEST_PORT) }

      before(:each) do
        iface.target_names = ['JSON_TLM_TEST']
        iface.connect
        sleep 0.1
      end

      after(:each) do
        iface.disconnect
      end

      context '200 OK' do
        it 'returns 200 for valid POST to known target/packet' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 25.5}')
          expect(resp.code).to eq('200')
        end

        it 'queues the request for convert_data_to_packet' do
          OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 42.0}')
          sleep 0.05
          queue = iface.instance_variable_get(:@request_queue)
          expect(queue.length).to be >= 1
        end
      end

      context '400 Bad Request' do
        it 'returns 400 for malformed JSON body' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', 'not-json')
          expect(resp.code).to eq('400')
        end

        it 'returns 400 for empty body' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '')
          expect(resp.code).to eq('400')
        end
      end

      context '404 Not Found' do
        it 'returns 404 for unknown target' do
          resp = OpenC3.http_post(TEST_PORT, '/UNKNOWN/SENSORS', '{"TEMPERATURE": 1.0}')
          expect(resp.code).to eq('404')
        end

        it 'returns 404 for unknown packet' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/UNKNOWN_PKT', '{"TEMPERATURE": 1.0}')
          expect(resp.code).to eq('404')
        end

        it 'returns 404 for too few path segments' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST', '{"TEMPERATURE": 1.0}')
          expect(resp.code).to eq('404')
        end
      end

      context '405 Method Not Allowed' do
        it 'returns 405 for GET' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '', {}, method: :Get)
          expect(resp.code).to eq('405')
        end

        it 'returns 405 for PUT' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '{}', {}, method: :Put)
          expect(resp.code).to eq('405')
        end

        it 'returns 405 for DELETE' do
          resp = OpenC3.http_post(TEST_PORT, '/JSON_TLM_TEST/SENSORS', '', {}, method: :Delete)
          expect(resp.code).to eq('405')
        end
      end
    end

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    describe 'API key authentication' do
      let(:auth_port) { TEST_PORT + 10 }
      let(:iface) do
        i = JsonTelemetryServerInterface.new(auth_port)
        i.set_option('API_KEY', ['secret-token-123'])
        i
      end

      before(:each) do
        iface.target_names = ['JSON_TLM_TEST']
        iface.connect
        sleep 0.1
      end

      after(:each) do
        iface.disconnect
      end

      it 'returns 200 with correct bearer token' do
        resp = OpenC3.http_post(auth_port, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 1.0}',
                                { 'Authorization' => 'Bearer secret-token-123' })
        expect(resp.code).to eq('200')
      end

      it 'returns 401 when Authorization header is missing' do
        resp = OpenC3.http_post(auth_port, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 1.0}')
        expect(resp.code).to eq('401')
      end

      it 'returns 401 with wrong token' do
        resp = OpenC3.http_post(auth_port, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 1.0}',
                                { 'Authorization' => 'Bearer wrong' })
        expect(resp.code).to eq('401')
      end

      it 'includes WWW-Authenticate: Bearer header on 401' do
        resp = OpenC3.http_post(auth_port, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 1.0}')
        expect(resp['www-authenticate']).to eq('Bearer')
      end

      it 'accepts all requests when no API_KEY is configured' do
        open_iface = JsonTelemetryServerInterface.new(auth_port + 1)
        open_iface.target_names = ['JSON_TLM_TEST']
        open_iface.connect
        sleep 0.1
        begin
          resp = OpenC3.http_post(auth_port + 1, '/JSON_TLM_TEST/SENSORS', '{"TEMPERATURE": 1.0}')
          expect(resp.code).to eq('200')
        ensure
          open_iface.disconnect
        end
      end

      it 'stores API_KEY and preserves LISTEN_ADDRESS via super' do
        i = JsonTelemetryServerInterface.new(TEST_PORT + 30)
        i.set_option('API_KEY', ['tok'])
        i.set_option('LISTEN_ADDRESS', ['127.0.0.1'])
        expect(i.instance_variable_get(:@api_key)).to eq('tok')
        expect(i.instance_variable_get(:@listen_address)).to eq('127.0.0.1')
      end
    end

    # ------------------------------------------------------------------
    # convert_data_to_packet
    # ------------------------------------------------------------------

    describe '#convert_data_to_packet' do
      let(:iface) { JsonTelemetryServerInterface.new(TEST_PORT + 20) }

      it 'returns a populated packet for valid JSON and known target/packet' do
        extra = {
          'HTTP_REQUEST_TARGET_NAME' => 'JSON_TLM_TEST',
          'HTTP_REQUEST_PACKET_NAME' => 'SENSORS',
        }
        data = '{"TEMPERATURE": 99.0}'
        pkt = iface.convert_data_to_packet(data, extra)
        expect(pkt.target_name).to eq('JSON_TLM_TEST')
        expect(pkt.packet_name).to eq('SENSORS')
        expect(pkt.read('TEMPERATURE')).to be_within(0.01).of(99.0)
      end

      it 'returns a bare packet when extra is nil' do
        pkt = iface.convert_data_to_packet('{}')
        expect(pkt.target_name).to be_nil
      end
    end
  end
end
